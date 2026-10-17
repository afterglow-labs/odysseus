"""Owner-scoped, durable main-prompt library shared by all workflow clients."""
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from src.constants import DATA_DIR

log = logging.getLogger(__name__)
DEFAULT_ROOT = Path(DATA_DIR) / "video_jobs" / ".prompt_history"
MAX_PROMPT_BYTES = 64 * 1024
BACKFILL_VERSION = 1
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


class PromptTooLarge(ValueError):
    pass


def normalize_prompt(value):
    if not isinstance(value, str):
        raise ValueError("Prompt must be text")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        raise ValueError("Enter a prompt to save")
    if len(value.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise PromptTooLarge("Prompt exceeds 64 KiB")
    return value


def _workflow(value):
    if value is None:
        return "workflow_graph"
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("Workflow must be a short text label")
    return value.strip() or "workflow_graph"


def _timestamp(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value < 253402300799:
        return float(value)
    return time.time()


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _entry(row):
    return {key: _iso(row[key]) if key in {"created_at", "last_used_at"} else row[key]
            for key in ("id", "prompt", "workflow", "created_at", "last_used_at")}


def _positive_graph_prompts(graph):
    """Only explicit main prompts and encoders wired to positive conditioning.

    Arbitrary STRING widgets, negative conditioning, and dialogue fragments are
    deliberately not inferred to be reusable main prompts.
    """
    if not isinstance(graph, dict):
        return []
    found, visited, negative_nodes = [], set(), set()
    def inputs_of(node):
        inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
        return inputs if isinstance(inputs, dict) else {}

    def linked(value):
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
            return str(value[0])
        return None

    # A negative encoder can call its text widget "prompt". Do not let that
    # alias accidentally become an independent positive prompt during scanning.
    pending = [inputs[key] for node in graph.values() for inputs in [inputs_of(node)]
               for key in ("negative", "negative_prompt") if key in inputs]
    while pending:
        identity = linked(pending.pop())
        if identity is not None and identity not in negative_nodes:
            negative_nodes.add(identity)
            pending.extend(inputs_of(graph.get(identity)).values())

    def positive(value):
        pending = [value]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                found.append(value)
                continue
            identity = linked(value)
            if identity is None or identity in visited:
                continue
            visited.add(identity)
            node = graph.get(identity, {})
            inputs = inputs_of(node)
            kind = str(node.get("class_type", "")).lower() if isinstance(node, dict) else ""
            keys = tuple(inputs) if "reroute" in kind else ("text", "text_g", "text_l", "prompt", "positive", "positive_prompt")
            pending.extend(inputs[key] for key in reversed(keys) if key in inputs)

    for node in graph.values():
        inputs = inputs_of(node)
        for key in ("positive", "positive_prompt"):
            if key in inputs:
                positive(inputs[key])
    for identity, node in graph.items():
        inputs = inputs_of(node)
        if "prompt" in inputs and (str(identity) not in negative_nodes or str(identity) in visited):
            positive(inputs["prompt"])
    return found


def config_prompts(config, *, family=None, draft=False):
    if not isinstance(config, dict):
        return []
    if family == "bfs" or "workflow_id" in config:
        # BFS's validated prompt includes instructions surrounding the editable
        # notes. Saving that assembled prompt would wrap it again when reused.
        value = config.get("prompt_notes", config.get("prompt") if draft else None)
        candidates, workflow = [value], "bfs"
    elif config.get("recipe") == "comfy_graph" or isinstance(config.get("prompt"), dict):
        candidates, workflow = _positive_graph_prompts(config.get("prompt")), "workflow_graph"
    else:
        workflow = "h3"
        if config.get("recipe") == "seed_hunter":
            options = config.get("seed_hunter", {})
            workflow = "seed_scout" if isinstance(options, dict) and options.get("stage") == "scout" else "seed_hunter"
        candidates = [config.get("prompt")]
    result = []
    for value in candidates:
        try:
            pair = (normalize_prompt(value), workflow)
        except (ValueError, UnicodeError):
            continue
        if pair not in result:
            result.append(pair)
    return result


def compiled_prompts(compiled):
    plan = compiled.get("plan", []) if isinstance(compiled, dict) else []
    result = []
    for step in plan if isinstance(plan, list) else []:
        if isinstance(step, dict) and step.get("family") in {"h3", "bfs", "comfy"}:
            result.extend(config_prompts(step.get("config"), family=step.get("family"), draft=True))
    return result


class PromptHistoryStore:
    def __init__(self, root=DEFAULT_ROOT, *, job_roots=None, graph_root=None):
        self.root = Path(root)
        self.path = self.root / "history.sqlite3"
        self.job_roots = list(job_roots) if job_roots is not None else [self.root.parent / name for name in ("h3", "bfs", "comfy")]
        self.graph_root = Path(graph_root) if graph_root is not None else (Path(DATA_DIR) / "workflow_graphs" / "runs" if self.root == DEFAULT_ROOT else None)

    def _connect(self):
        if self.root.is_symlink() or self.path.is_symlink():
            raise OSError("Prompt history storage must not be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=15)
        try:
            self.path.chmod(0o600)
            db.row_factory = sqlite3.Row
            db.executescript("""
            CREATE TABLE IF NOT EXISTS prompts (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, prompt TEXT NOT NULL COLLATE BINARY,
                workflow TEXT NOT NULL, created_at REAL NOT NULL, last_used_at REAL NOT NULL,
                UNIQUE(owner, prompt)
            );
            CREATE INDEX IF NOT EXISTS prompts_owner_used ON prompts(owner, last_used_at DESC, id);
            CREATE TABLE IF NOT EXISTS backfill (owner TEXT PRIMARY KEY, version INTEGER NOT NULL);
            """)
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _owner(owner):
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Prompt history requires an owner")
        return owner

    @staticmethod
    def _save(db, owner, prompt, workflow, used):
        row = db.execute("SELECT * FROM prompts WHERE owner=? AND prompt=?", (owner, prompt)).fetchone()
        if row is None:
            identity = uuid.uuid4().hex
            db.execute("INSERT INTO prompts VALUES (?,?,?,?,?,?)", (identity, owner, prompt, workflow, used, used))
        else:
            identity = row["id"]
            db.execute("UPDATE prompts SET created_at=?, last_used_at=?, workflow=? WHERE id=?",
                       (min(used, row["created_at"]), max(used, row["last_used_at"]),
                        workflow if used >= row["last_used_at"] else row["workflow"], identity))
        saved = db.execute("SELECT * FROM prompts WHERE id=?", (identity,)).fetchone()
        return {"entry": _entry(saved), "created": row is None}

    def record(self, owner, prompt, workflow=None, *, at=None):
        owner, prompt, workflow = self._owner(owner), normalize_prompt(prompt), _workflow(workflow)
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                return self._save(db, owner, prompt, workflow, _timestamp(at))
        finally:
            db.close()

    def record_many(self, owner, prompts, *, at=None):
        owner = self._owner(owner)
        pairs = dict((normalize_prompt(prompt), _workflow(workflow)) for prompt, workflow in prompts)
        if not pairs:
            return
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                stamp = _timestamp(at)
                for prompt, workflow in pairs.items():
                    self._save(db, owner, prompt, workflow, stamp)
        finally:
            db.close()

    @staticmethod
    def _json(path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def _historical(self, owner):
        roots = [(Path(root), False) for root in self.job_roots]
        if self.graph_root is not None:
            roots.append((self.graph_root, True))
        for root, graph in roots:
            if root.is_symlink() or not root.is_dir():
                continue
            for directory in root.iterdir():
                if not JOB_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                    continue
                try:
                    state = self._json(directory / "state.json")
                    if state.get("owner") != owner:
                        continue
                    manifest = self._json(directory / "manifest.json")
                    prompts = compiled_prompts(manifest.get("compiled")) if graph else config_prompts(manifest.get("config"), family=root.name)
                    for prompt, workflow in prompts:
                        yield prompt, workflow, _timestamp(state.get("created_at"))
                except (OSError, ValueError, UnicodeError):
                    continue

    def backfill(self, owner):
        owner = self._owner(owner)
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                done = db.execute("SELECT version FROM backfill WHERE owner=?", (owner,)).fetchone()
                if done and done[0] >= BACKFILL_VERSION:
                    return
                for prompt, workflow, used in self._historical(owner):
                    self._save(db, owner, prompt, workflow, used)
                db.execute("INSERT OR REPLACE INTO backfill VALUES (?,?)", (owner, BACKFILL_VERSION))
        finally:
            db.close()

    def list(self, owner, *, q="", limit=100, offset=0):
        owner = self._owner(owner)
        if not isinstance(q, str) or len(q) > 1000:
            raise ValueError("Search must be at most 1000 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Limit must be between 1 and 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 2**31 - 1:
            raise ValueError("Offset must be a nonnegative integer")
        self.backfill(owner)
        db = self._connect()
        try:
            # INSTR treats %, _ and quotes as literal search text.
            where, args = "owner=? AND instr(lower(prompt), lower(?)) > 0", (owner, q)
            with db:
                db.execute("BEGIN")
                total = db.execute("SELECT count(*) FROM prompts WHERE " + where, args).fetchone()[0]
                rows = db.execute("SELECT * FROM prompts WHERE " + where + " ORDER BY last_used_at DESC, id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            return {"prompts": [_entry(row) for row in rows], "total": total,
                    "next_offset": offset + len(rows) if offset + len(rows) < total else None}
        finally:
            db.close()


def remember_job_prompts(manager, owner, configs):
    """Bookkeeping cannot turn an accepted job/edit into a retryable failure."""
    try:
        prompts = [pair for config in configs for pair in config_prompts(config)]
        if prompts:
            PromptHistoryStore(manager.root.parent / ".prompt_history").record_many(owner, prompts)
    except Exception:
        log.warning("Could not save accepted workflow prompt history", exc_info=True)


def remember_graph_prompts(manager, owner, compiled):
    try:
        prompts = compiled_prompts(compiled)
        if not prompts:
            return
        native = next(iter(manager.managers.values()), None)
        root = native.root.parent if native is not None else manager.root.parent
        PromptHistoryStore(root / ".prompt_history").record_many(owner, prompts)
    except Exception:
        log.warning("Could not save accepted graph prompt history", exc_info=True)
