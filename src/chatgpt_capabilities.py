"""Account-scoped model options; no inference or credential storage.

Catalog shape follows OpenAI Codex's ModelAccessPrograms:
https://github.com/openai/codex/blob/main/codex-rs/protocol/src/openai_models/access_programs.rs
"""

from collections import OrderedDict
import hashlib
import json
import logging
import threading
import time

_lock = threading.RLock()
_token_accounts = OrderedDict()
_pending_catalogs = OrderedDict()
_generation = 0
logger = logging.getLogger(__name__)


def _database_handles():
    from core.database import ProviderAuthSession, SessionLocal
    return ProviderAuthSession, SessionLocal


def _fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest() if token else ""


def _bounded_put(cache, key, value, limit=256):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _decode(raw):
    try:
        value = json.loads(raw or "{}")
        if isinstance(value, dict) and isinstance(value.get("models"), dict):
            return value
    except (ValueError, TypeError):
        pass
    return {"revision": 0, "models": {}}


def capability_generation():
    with _lock:
        return _generation


def _update(account_id, update):
    """Serialize account metadata changes without loading encrypted tokens."""
    global _generation
    if not account_id:
        return
    ProviderAuthSession, SessionLocal = _database_handles()
    with _lock, SessionLocal() as db:
        row = db.query(ProviderAuthSession.id, ProviderAuthSession.model_capabilities).filter(
            ProviderAuthSession.id == account_id,
            ProviderAuthSession.provider == "chatgpt-subscription",
        ).first()
        if row is None:
            return
        value = update(_decode(row.model_capabilities))
        db.query(ProviderAuthSession).filter(ProviderAuthSession.id == account_id).update(
            {ProviderAuthSession.model_capabilities: json.dumps(value)}, synchronize_session=False,
        )
        db.commit()
        _generation += 1


def bind_account_token(account_id, token):
    """Bind an already-resolved runtime credential; never retain the token."""
    key = _fingerprint(token)
    if not account_id or not key:
        return
    with _lock:
        for previous in [digest for digest, account in _token_accounts.items() if account == account_id and digest != key]:
            del _token_accounts[previous]
        _bounded_put(_token_accounts, key, account_id)
        pending = _pending_catalogs.pop(key, None)
        if pending is not None:
            try:
                _save_catalog(account_id, *pending)
            except Exception:
                logger.warning("Could not save subscription catalog metadata")


def _catalog_models(entries):
    from src.chatgpt_subscription import daybreak_access_program
    models = {}
    for item in entries[:2000]:
        if not isinstance(item, dict):
            continue
        model = item.get("slug")
        if not isinstance(model, str) or not model.strip():
            continue
        model = model.strip()
        programs = item.get("available_access_programs")
        cyber = programs.get("cyber") if isinstance(programs, dict) else None
        supported = None
        if isinstance(cyber, list) and all(isinstance(value, str) for value in cyber):
            supported = daybreak_access_program(model, True) in cyber
        models[model] = {
            "supported": supported,
            "reason": ("Daybreak is not available for this model on this account."
                       if supported is False else ""),
            "source": "catalog" if supported is not None else "unknown",
        }
    return models


def _catalog_reasoning_models(entries):
    from src.chatgpt_subscription import normalize_reasoning_effort
    result = {}
    for item in entries[:2000]:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str) or not item["slug"].strip():
            continue
        raw = item.get("supported_reasoning_levels")
        levels, supported = [], None
        if isinstance(raw, list):
            try:
                for preset in raw:
                    if not isinstance(preset, dict):
                        raise ValueError("Invalid reasoning preset")
                    effort = normalize_reasoning_effort(preset.get("effort"))
                    if effort is None:
                        raise ValueError("Missing reasoning effort")
                    if effort not in levels:
                        levels.append(effort)
                supported = bool(levels)
            except ValueError:
                levels = []
        try:
            default = normalize_reasoning_effort(item.get("default_reasoning_level"))
        except ValueError:
            default = None
        if supported is not None and default not in levels:
            default = None
        result[item["slug"].strip()] = {"levels": levels, "default": default, "supported": supported}
    return result


def _save_catalog(account_id, models, reasoning_models):
    def update(previous):
        return {**previous,
                "revision": max(int(previous.get("revision") or 0) + 1, int(time.time() * 1000)),
                "models": models, "reasoning_models": reasoning_models}
    _update(account_id, update)


def record_model_catalog(token, entries):
    """Replace metadata only after a valid successful catalog response."""
    key = _fingerprint(token)
    if not key or not isinstance(entries, list):
        return
    models = _catalog_models(entries)
    reasoning_models = _catalog_reasoning_models(entries)
    with _lock:
        account_id = _token_accounts.get(key)
        if account_id:
            try:
                _save_catalog(account_id, models, reasoning_models)
            except Exception:
                logger.warning("Could not save subscription catalog metadata")
        else:
            _bounded_put(_pending_catalogs, key, (models, reasoning_models), limit=32)


def record_daybreak_model_denial(account_id, model, reason):
    """Record an explicit model/program denial, never an account auth failure."""
    if not isinstance(model, str) or not model:
        return
    def update(previous):
        previous["models"][model] = {"supported": False, "reason": str(reason)[:700], "source": "upstream"}
        return previous
    _update(account_id, update)


def record_request_denial(headers, model, program_error):
    if not program_error or program_error[0] != "unsupported_access_program":
        return
    bearer = next((value for name, value in (headers or {}).items() if name.lower() == "authorization"), "")
    token = bearer[7:] if isinstance(bearer, str) and bearer.lower().startswith("bearer ") else ""
    with _lock:
        account_id = _token_accounts.get(_fingerprint(token))
    if account_id:
        try:
            record_daybreak_model_denial(account_id, model, program_error[1])
        except Exception:
            # Metadata persistence must not replace the actual provider error.
            logger.warning("Could not save subscription model capability", exc_info=True)


def daybreak_snapshot(account_id, model_ids):
    value = {"revision": 0, "models": {}}
    if account_id:
        ProviderAuthSession, SessionLocal = _database_handles()
        with SessionLocal() as db:
            row = db.query(ProviderAuthSession.model_capabilities).filter(
                ProviderAuthSession.id == account_id,
                ProviderAuthSession.provider == "chatgpt-subscription",
            ).first()
            if row is not None:
                value = _decode(row.model_capabilities)
    return {
        "daybreak_revision": value.get("revision", 0),
        "daybreak_models": {model: dict(value["models"].get(model) or {
            "supported": None, "reason": "", "source": "unknown",
        }) for model in model_ids},
        "reasoning_models": {model: dict((value.get("reasoning_models") or {}).get(model) or {
            "levels": [], "default": None, "supported": None,
        }) for model in model_ids},
    }


def validate_reasoning_effort(value, model, *, account_id=None, headers=None):
    """Validate explicit effort against cached account metadata; no network."""
    from src.chatgpt_subscription import normalize_reasoning_effort
    effort = normalize_reasoning_effort(value)
    if effort is None:
        return None
    if account_id is None:
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except ValueError:
                headers = None
        if isinstance(headers, dict):
            bearer = next((value for name, value in headers.items() if name.lower() == "authorization"), "")
            token = bearer[7:] if isinstance(bearer, str) and bearer.lower().startswith("bearer ") else ""
            with _lock:
                account_id = _token_accounts.get(_fingerprint(token))
    metadata = daybreak_snapshot(account_id, [model])["reasoning_models"][model]
    if metadata["supported"] is not None and effort not in metadata["levels"]:
        raise ValueError(f"Reasoning effort '{effort}' is not supported by {model} on this account. Choose a listed effort or provider default.")
    return effort
