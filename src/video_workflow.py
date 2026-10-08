"""Portable data-only video workflows with bounded, owner-scoped ZIP transfers."""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import stat
import struct
import threading
import time
import uuid
import zipfile
import zlib

from src.h3_video import _locked, _read, _write
from src.h3_loras import MAX_LORAS, lora_entries
from src.h3_vfx import normalize_vfx_inputs

FORMAT, VERSION = "odysseus-video-workflow", 1
INPUT_LAYOUT = "source-guide-v1"
CHUNK, JSON_LIMIT, MAX_ENTRIES, TTL = 1024 * 1024, 128 * 1024, 64, 7200
RESERVE_BYTES = 512 * 1024 * 1024
ID = re.compile(r"^[a-f0-9]{32}$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
H3_SETTINGS = {"mode", "width", "height", "frames", "steps", "seed", "sampler",
               "scheduler", "shift_video", "shift_audio", "lora_scale", "reference_size", "prompt", "lora_strengths"}


def safe_path(value, basename=False):
    if (not isinstance(value, str) or not value or len(value) > 240
            or any(ord(c) < 32 for c in value) or "\\" in value or ":" in value
            or any(p in ("", ".", "..") for p in value.split("/"))
            or basename and "/" in value):
        raise ValueError("Workflow filenames must be relative and cannot traverse directories")
    return value


def specification(family, config):
    if family == "h3":
        from src.h3_video import UPLOAD_EXTENSIONS
        roles = {"model", "encoder", "video_vae", "audio_vae"}
        if "lora_strengths" in config:
            strengths = config["lora_strengths"]
            if (not isinstance(strengths, list) or len(strengths) > MAX_LORAS or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or not -4 <= value <= 4 for value in strengths)):
                raise ValueError("Invalid portable LoRA strengths")
            roles |= {f"lora_{index}" for index in range(len(strengths))}
        else:
            roles.add("lora")
        return H3_SETTINGS, roles, UPLOAD_EXTENSIONS
    if family == "bfs":
        from src.bfs_video import BY_ID, UPLOAD_EXTENSIONS
        identity = config.get("workflow_id")
        item = BY_ID.get(identity) if isinstance(identity, str) else None
        if item is None:
            raise ValueError("Choose an available BFS workflow")
        return ({"workflow_id", "prompt"} | {c["key"] for c in item["controls"]},
                {s["key"] for s in item["slots"]}, UPLOAD_EXTENSIONS)
    raise ValueError("Unknown video workflow family")


def portable_config(raw, family):
    if not isinstance(raw, dict):
        raise ValueError("Workflow config must be an object")
    settings, _, _ = specification(family, raw)
    if raw.keys() - settings:
        raise ValueError("Unknown portable workflow setting")
    for key, value in raw.items():
        if family == "h3" and key == "lora_strengths":
            continue  # Bounded and validated by specification above.
        if not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Workflow settings must contain simple finite values")
        if isinstance(value, str) and len(value) > (16000 if key == "prompt" else 128):
            raise ValueError("A workflow setting is too long")
    if family == "h3" and raw.get("mode", "t2va") not in ("t2va", "fl2va", "ref2va"):
        raise ValueError("Unknown H3 workflow mode")
    return dict(raw)


def component_reference(path):
    from src.model_library import component_reference as named_reference
    named = named_reference(path)
    if named:
        return named
    path = Path(path)
    result = {"name": safe_path(path.name, True)}
    for candidate in (path, path.resolve()):
        parts = candidate.parts
        for index, part in enumerate(parts):
            if not part.startswith("models--") or len(parts) <= index + 3 or parts[index + 1] != "snapshots":
                continue
            if re.fullmatch(r"odysseus-[a-f0-9]{32}", parts[index + 2]):
                # A local content-addressed revision is not a Hugging Face
                # commit. Restore its original download identity on re-export.
                provenance = provenance_path(candidate)
                try:
                    if provenance.stat().st_size <= 16 * 1024:
                        saved = _read(provenance)
                        original = saved.get("reference")
                        if (isinstance(original, dict) and original.get("name") == result["name"]
                                and "archive_path" not in original
                                and saved.get("sha256", "").startswith(parts[index + 2][9:])):
                            validate_document({"format": FORMAT, "version": VERSION, "family": "h3",
                                               "config": {}, "components": {"model": original}, "attachments": []}, "h3")
                            return dict(original)
                except (OSError, ValueError, TypeError):
                    pass
                return result
            repository = "/".join(part[8:].split("--", 1))
            if REPO.fullmatch(repository):
                result.update(repository=repository, relative_path="/".join(parts[index + 3:]))
                if REVISION.fullmatch(parts[index + 2]):
                    result["revision"] = parts[index + 2]
                return result
    return result


def provenance_path(path):
    path = Path(path)
    identity = hashlib.sha256(path.name.encode()).hexdigest()[:16]
    return path.with_name(".odysseus-provenance-" + identity + ".json")


def export_options(raw):
    if not isinstance(raw, dict) or raw.keys() - {"include_weights", "include_attachments"}:
        raise ValueError("Invalid workflow export options")
    result = {"include_weights": False, "include_attachments": False} | raw
    if any(type(value) is not bool for value in result.values()):
        raise ValueError("Export options must be true or false")
    return result


def validate_document(value, family):
    if (not isinstance(value, dict) or value.get("format") != FORMAT
            or type(value.get("version")) is not int or value["version"] != VERSION):
        raise ValueError("Unsupported Odysseus workflow format or version")
    if value.get("family") != family:
        raise ValueError("Import this workflow into its matching H3 or BFS editor")
    if value.keys() - {"format", "version", "family", "config", "components", "attachments", "input_layout"}:
        raise ValueError("Unknown workflow metadata")
    if "input_layout" in value and (family != "h3" or value["input_layout"] != INPUT_LAYOUT):
        raise ValueError("Unknown workflow input layout")
    portable_config(value.get("config"), family)
    _, roles, extensions = specification(family, value["config"])
    components, attachments = value.get("components"), value.get("attachments")
    if not isinstance(components, dict) or components.keys() - roles or not isinstance(attachments, list):
        raise ValueError("Invalid workflow components or attachments")
    if family == "h3" and "lora_strengths" in value["config"]:
        if not {f"lora_{index}" for index in range(len(value["config"]["lora_strengths"]))} <= components.keys():
            raise ValueError("A stacked LoRA reference is missing")
    declared, counts = {"workflow.json"}, {}
    for item in components.values():
        if not isinstance(item, dict) or item.keys() - {"name", "repository", "relative_path", "revision", "archive_path"}:
            raise ValueError("Invalid model download reference")
        name = safe_path(item.get("name"), True)
        if not name.lower().endswith(".safetensors"):
            raise ValueError("Only safetensors weights can be transferred")
        if "repository" in item and (not isinstance(item["repository"], str) or not REPO.fullmatch(item["repository"])):
            raise ValueError("Invalid model repository")
        if "revision" in item and (not isinstance(item["revision"], str) or not REVISION.fullmatch(item["revision"])):
            raise ValueError("Invalid model revision")
        if "relative_path" in item:
            if PurePosixPath(safe_path(item["relative_path"])).name != name:
                raise ValueError("Model download path does not match its filename")
        if "archive_path" in item:
            relative = safe_path(item["archive_path"])
            if not relative.startswith("weights/") or PurePosixPath(relative).name != name or relative in declared:
                raise ValueError("Invalid or duplicate archived model path")
            declared.add(relative)
    for item in attachments:
        if not isinstance(item, dict) or set(item) != {"field", "name", "path", "size"}:
            raise ValueError("Invalid workflow attachment")
        field = item["field"]
        if not isinstance(field, str) or field not in extensions:
            raise ValueError("Invalid attachment role")
        name, relative = safe_path(item["name"], True), safe_path(item["path"])
        if (not relative.startswith("attachments/") or relative in declared
                or Path(name).suffix.lower() not in extensions[field]
                or Path(relative).suffix.lower() != Path(name).suffix.lower()):
            raise ValueError("Unsupported or duplicate workflow attachment")
        if type(item["size"]) is not int or item["size"] <= 0:
            raise ValueError("Invalid attachment size")
        counts[field] = counts.get(field, 0) + 1
        if counts[field] > {"reference_images": 9, "reference_videos": 3, "reference_audio": 3}.get(field, 1):
            raise ValueError("Too many workflow attachments")
        declared.add(relative)
    if family == "h3":
        mode = value["config"].get("mode", "t2va")
        refs = sum(counts.get(k, 0) for k in ("reference_images", "reference_videos", "reference_audio"))
        if (refs > 12 or mode == "t2va" and counts
                or mode == "ref2va" and (counts.get("first_frame") or counts.get("last_frame"))
                or mode == "fl2va" and (refs or counts.get("source_video"))):
            raise ValueError("Attachments do not match the H3 workflow mode")
    else:
        from src.bfs_video import BY_ID
        if any(field not in BY_ID[value["config"]["workflow_id"]]["source_requirements"] for field in counts):
            raise ValueError("Attachments do not match the BFS workflow")
    return declared


def normalize_portable_inputs(document):
    """Map legacy VFX attachment roles without renaming their ZIP members."""
    if document["family"] != "h3":
        return document
    result = {**document, "input_layout": INPUT_LAYOUT}
    if "input_layout" in document:
        return result
    config, components = document["config"], document["components"]
    if "lora_strengths" in config:
        canonical = {"loras": [{"path": components[f"lora_{i}"]["name"], "strength": strength}
                                for i, strength in enumerate(config["lora_strengths"])]}
    else:
        canonical = {"lora": components.get("lora", {}).get("name", ""), "lora_scale": config.get("lora_scale", 1)}
    grouped = {}
    for item in document["attachments"]:
        grouped.setdefault(item["field"], []).append(item)
    normalized = normalize_vfx_inputs(canonical, grouped)
    if "source_video" not in grouped and normalized.get("source_video"):
        promoted = normalized["source_video"][0]
        result["attachments"] = [{**item, "field": "source_video"} if item is promoted else dict(item)
                                 for item in document["attachments"]]
    return result


def resolve_draft(raw, inventory, family):
    if not isinstance(raw, dict):
        raise ValueError("Workflow config must be an object")
    settings, roles, _ = specification(family, raw)
    if raw.keys() - (settings | {"gpu", "vae_gpu"} | (roles | {"loras"} if family == "h3" else {"components"})):
        raise ValueError("Unknown workflow export setting")
    identities = {r: raw.get(r) for r in roles if r != "lora" or "loras" not in raw} if family == "h3" else raw.get("components", {})
    if not isinstance(identities, dict) or identities.keys() - roles:
        raise ValueError("Invalid component selections")
    available, selected = {row["id"]: row for row in inventory["components"]}, {}
    for role, identity in identities.items():
        if identity in (None, ""):
            continue
        item = available.get(identity) if isinstance(identity, str) else None
        if item is None:
            raise ValueError("A selected model is no longer available")
        if not compatible(item, role, family, raw):
            raise ValueError("A selected model has the wrong component role")
        selected[role] = item["path"]
    config = {k: v for k, v in raw.items() if k in settings}
    if family == "h3" and "loras" in raw:
        rows = lora_entries(raw)
        config["lora_strengths"] = [row["strength"] for row in rows]
        paths = set()
        for index, row in enumerate(rows):
            item = available.get(row["id"])
            if not item or item.get("role") != "lora":
                raise ValueError("A selected LoRA is no longer available")
            path = Path(item["path"]).resolve()
            if path in paths:
                raise ValueError("The same LoRA file cannot be selected more than once")
            paths.add(path)
            selected[f"lora_{index}"] = item["path"]
    return config, selected


def compatible(item, role, family, config):
    if family == "h3":
        return item.get("role") == ("lora" if re.fullmatch(r"lora_[0-7]", role) else role)
    from src.bfs_video import BY_ID, matches
    definition = BY_ID[config["workflow_id"]]
    slot = next(s for s in definition["slots"] if s["key"] == role)
    return matches(item, slot, definition)


def make_recipe(config, selected, uploads, family, options):
    if family == "h3" and "loras" in config:
        rows = lora_entries(config, "path")
        config = {**config, "lora_strengths": [row["strength"] for row in rows]}
        selected = {key: value for key, value in selected.items() if key != "lora"}
        selected.update({f"lora_{index}": row["path"] for index, row in enumerate(rows)})
    settings, roles, extensions = specification(family, config)
    document = {"format": FORMAT, "version": VERSION, "family": family,
                "config": portable_config({k: v for k, v in config.items() if k in settings}, family),
                "components": {}, "attachments": []}
    if family == "h3":
        document["input_layout"] = INPUT_LAYOUT
    assets = []
    for role, value in selected.items():
        if role not in roles or not value:
            continue
        path = Path(value)
        if path.suffix.lower() != ".safetensors" or options["include_weights"] and not path.is_file():
            raise ValueError("A selected weight is unavailable")
        item = component_reference(path)
        if options["include_weights"]:
            relative = "weights/" + role + "/" + item["name"]
            item["archive_path"] = relative
            assets.append({"path": str(path.resolve()), "archive_path": relative, "size": path.stat().st_size})
        document["components"][role] = item
    if options["include_attachments"]:
        for field, values in uploads.items():
            if field not in extensions:
                raise ValueError("Unknown workflow attachment")
            for index, value in enumerate(values):
                path = Path(value["path"])
                name = safe_path(value.get("name") or path.name, True)
                relative = "attachments/" + field + "-" + str(index + 1) + path.suffix.lower()
                size = path.stat().st_size
                document["attachments"].append({"field": field, "name": name, "path": relative, "size": size})
                assets.append({"path": str(path), "archive_path": relative, "size": size})
    validate_document(document, family)
    return document, assets


def free_space(path, amount):
    if amount < 0 or shutil.disk_usage(path).free < amount + RESERVE_BYTES:
        raise OSError("Not enough disk space for this workflow transfer; 512 MiB must remain free")


def open_assets(assets):
    handles = []
    try:
        for asset in assets:
            stream = Path(asset["path"]).open("rb")
            handles.append((asset, stream))
            if os.fstat(stream.fileno()).st_size != asset["size"]:
                raise ValueError("A source file changed; export the workflow again")
        return handles
    except BaseException:
        for _, stream in handles:
            stream.close()
        raise


class _Cancelled(Exception):
    pass


def stream_zip(document, handles):
    """Use a nonseekable ZipFile sink, bounded to four queued 1 MiB chunks."""
    pending, stopped = queue.Queue(maxsize=4), threading.Event()
    def put(value):
        while not stopped.is_set():
            try:
                pending.put(value, timeout=.2)
                return
            except queue.Full:
                pass
        raise _Cancelled()
    class Sink:
        position = 0
        failed = False
        def write(self, value):
            if self.failed:
                # ZipFile closes even after a source fails. Do not publish a
                # central directory that makes its partial payload look valid.
                return len(value)
            for start in range(0, len(value), CHUNK):
                put(bytes(value[start:start + CHUNK]))
            self.position += len(value)
            return len(value)
        def tell(self):
            return self.position
        def seek(self, *_):
            raise OSError("stream")
        def flush(self):
            pass
    def produce():
        sink = Sink()
        try:
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                try:
                    archive.writestr("workflow.json", json.dumps(document, ensure_ascii=False, indent=2))
                    for asset, source in handles:
                        info = zipfile.ZipInfo(asset["archive_path"])
                        info.file_size = asset["size"]
                        info.external_attr = (stat.S_IFREG | 0o600) << 16
                        with archive.open(info, "w", force_zip64=True) as output:
                            count = 0
                            while data := source.read(CHUNK):
                                if stopped.is_set():
                                    raise _Cancelled()
                                count += len(data)
                                if count > asset["size"]:
                                    raise ValueError("A workflow source changed during download")
                                output.write(data)
                            if count != asset["size"]:
                                raise ValueError("A workflow source changed during download")
                except BaseException:
                    sink.failed = True
                    raise
            put(None)
        except _Cancelled:
            pass
        except BaseException as exc:
            if not stopped.is_set():
                try:
                    put(exc)
                except _Cancelled:
                    pass
        finally:
            for _, source in handles:
                source.close()
    threading.Thread(target=produce, daemon=True, name="workflow-zip").start()
    try:
        while True:
            value = pending.get()
            if value is None:
                break
            if isinstance(value, BaseException):
                raise value
            yield value
    finally:
        stopped.set()


def job_export(manager, job_id, owner, family, options):
    manager._state(job_id, owner)
    directory = manager.directory(job_id)
    with _locked(directory / "job.lock"):
        # _state itself takes job.lock for active jobs: never nest that call.
        state = _read(directory / "state.json")
        if not state or state.get("owner") != owner:
            raise FileNotFoundError("Video job not found")
        manifest = _read(directory / "manifest.json")
        config = manifest.get("config")
        if not isinstance(config, dict):
            raise ValueError("This job has no usable workflow config")
        _, roles, extensions = specification(family, config)
        selected = {r: config[r] for r in roles if config.get(r)}
        uploads = {}
        if options["include_attachments"]:
            from src.video_job_edit import _inputs
            uploads = _inputs(directory, manifest, extensions)
        document, assets = make_recipe(config, selected, uploads, family, options)
        # Descriptors are opened while deletion is locked; an ongoing download
        # stays valid if its job is later removed.
        return document, open_assets(assets)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while data := source.read(CHUNK):
            digest.update(data)
    return digest.hexdigest()


def validate_safetensors(path):
    """Inspect only a bounded JSON header; never deserialize tensor payloads."""
    size = path.stat().st_size
    with path.open("rb") as source:
        raw = source.read(8)
        length = struct.unpack("<Q", raw)[0] if len(raw) == 8 else 0
        if not 0 < length <= 16 * CHUNK or length + 8 > size:
            raise ValueError("Invalid safetensors header")
        header = json.loads(source.read(length))
    if not isinstance(header, dict) or not any(k != "__metadata__" for k in header):
        raise ValueError("A safetensors file must contain tensors")
    for key, value in header.items():
        if key == "__metadata__":
            if not isinstance(value, dict):
                raise ValueError("Invalid safetensors metadata")
            continue
        if not isinstance(value, dict):
            raise ValueError("Invalid safetensors tensor")
        offsets, shape = value.get("data_offsets"), value.get("shape")
        if (not isinstance(value.get("dtype"), str) or not isinstance(shape, list)
                or any(type(n) is not int or n < 0 for n in shape)
                or not isinstance(offsets, list) or len(offsets) != 2
                or any(type(n) is not int for n in offsets)
                or not 0 <= offsets[0] <= offsets[1] <= size - length - 8):
            raise ValueError("Invalid safetensors tensor offsets")


class WorkflowTransfers:
    def __init__(self, manager, family):
        self.manager, self.family = manager, family
        self.root = manager.root.parent / "workflow_transfers" / family
        self.cache = Path(manager.project) / "cache/huggingface/hub"

    def stage(self, owner):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cleanup()
        directory = self.root / uuid.uuid4().hex
        directory.mkdir(mode=0o700)
        _write(directory / "receipt.json", {"owner": owner, "kind": "pending", "expires": time.time() + TTL})
        return directory

    def cleanup(self):
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not ID.fullmatch(directory.name):
                continue
            receipt = _read(directory / "receipt.json")
            if receipt.get("expires", directory.stat().st_mtime + TTL) > time.time():
                continue
            try:
                with _locked(directory / "lease.lock", blocking=False) as lease:
                    if lease is not None:
                        shutil.rmtree(directory, ignore_errors=True)
            except FileNotFoundError:
                pass

    def receipt(self, ticket, owner, kind):
        if not isinstance(ticket, str) or not ID.fullmatch(ticket):
            raise FileNotFoundError("Workflow transfer not found")
        directory = self.root / ticket
        if directory.is_symlink():
            raise FileNotFoundError("Workflow transfer not found")
        receipt = _read(directory / "receipt.json")
        if receipt.get("owner") != owner or receipt.get("kind") != kind or receipt.get("expires", 0) <= time.time():
            raise FileNotFoundError("Workflow transfer not found or expired")
        return directory, receipt

    def finish_export(self, directory, owner, raw, inventory, uploads, options):
        config, selected = resolve_draft(raw, inventory, self.family)
        document, assets = make_recipe(config, selected, uploads, self.family, options)
        _write(directory / "receipt.json", {"owner": owner, "kind": "export", "expires": time.time() + TTL,
                                            "document": document, "assets": assets})
        return {"download_url": f"/api/video/{self.family}/workflow/exports/{directory.name}",
                "filename": self.family + ".odysseus-workflow.zip",
                "total_bytes": sum(item["size"] for item in assets) + len(json.dumps(document).encode())}

    def export(self, ticket, owner):
        directory, receipt = self.receipt(ticket, owner, "export")
        with _locked(directory / "lease.lock"):
            self.receipt(ticket, owner, "export")
            return receipt["document"], open_assets(receipt["assets"])

    def attachment(self, ticket, index, owner):
        directory, receipt = self.receipt(ticket, owner, "import")
        attachments = receipt["document"]["attachments"]
        if index < 0 or index >= len(attachments):
            raise FileNotFoundError("Workflow attachment not found")
        item = attachments[index]
        path = directory / item["path"]
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
            raise FileNotFoundError("Workflow attachment not found")
        return path, item["name"]

    def _finish_import(self, directory, owner, document, weights):
        document = normalize_portable_inputs(document)
        inventory = self.manager.inventory()
        resolved = {}
        for role, reference in document["components"].items():
            candidates = []
            for item in inventory["components"]:
                if role in weights:
                    match = Path(item["path"]).resolve() == Path(weights[role]).resolve()
                else:
                    known = component_reference(item["path"])
                    match = known["name"] == reference["name"] and all(known.get(k) == reference[k] for k in ("repository", "relative_path", "revision") if k in reference)
                if match and compatible(item, role, self.family, document["config"]):
                    candidates.append(item["id"])
            if len(candidates) == 1:
                resolved[role] = candidates[0]
        _write(directory / "receipt.json", {"owner": owner, "kind": "import", "expires": time.time() + TTL, "document": document})
        attachments = [{"field": item["field"], "name": item["name"],
                        "url": f"/api/video/{self.family}/workflow/imports/{directory.name}/attachments/{i}"}
                       for i, item in enumerate(document["attachments"])]
        return {"workflow": document, "inventory": inventory, "attachments": attachments, "resolved_components": resolved}


    def import_file(self, directory, owner, source):
        """Read data only, validate all payloads, then publish private cache files."""
        if source.stat().st_size <= JSON_LIMIT:
            raw = source.read_bytes()
            if raw.lstrip().startswith(b"{"):
                document = json.loads(raw)
                validate_document(document, self.family)
                references_bundle = bool(document["attachments"]) or any("archive_path" in x for x in document["components"].values())
                # A workflow.json extracted from a bundle still restores a
                # useful draft, but must never pretend its absent bytes arrived.
                document["attachments"] = []
                for reference in document["components"].values():
                    reference.pop("archive_path", None)
                source.unlink()
                result = self._finish_import(directory, owner, document, {})
                if references_bundle:
                    result["warnings"] = ["Imported settings and model references from JSON. Import the ZIP to restore its bundled model weights and input media."]
                return result
        try:
            # ZipFile otherwise parses the entire central directory before we
            # can reject excessive entry counts. Bound that allocation first,
            # including ZIP64 counts used by multi-gigabyte model bundles.
            with source.open("rb") as archive_source:
                ending = zipfile._EndRecData(archive_source)
            if ending is None:
                raise zipfile.BadZipFile("Missing ZIP directory")
            if (ending[zipfile._ECD_ENTRIES_TOTAL] > MAX_ENTRIES
                    or ending[zipfile._ECD_SIZE] > 256 * 1024):
                raise ValueError("Workflow ZIP directory is too large")
            with zipfile.ZipFile(source) as archive:
                return self._import_zip(directory, owner, source, archive)
        except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
            raise ValueError("Use a complete, valid Odysseus workflow ZIP or workflow.json") from exc

    def _import_zip(self, directory, owner, source, archive):
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError("Too many workflow archive entries")
        names = set()
        for info in infos:
            safe_path(info.filename)
            mode = info.external_attr >> 16
            if (info.filename in names or info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or info.flag_bits & 1):
                raise ValueError("Duplicate, encrypted, or non-regular workflow archive entry")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or info.file_size < 0:
                raise ValueError("Unsupported workflow ZIP encoding")
            names.add(info.filename)
        if "workflow.json" not in names or archive.getinfo("workflow.json").file_size > JSON_LIMIT:
            raise ValueError("Missing or excessive workflow.json")
        document = json.loads(archive.read("workflow.json"))
        if names != validate_document(document, self.family):
            raise ValueError("Archive has missing or undeclared files; executable content is not allowed")
        # Account for inflated bytes before extraction, not just the ZIP upload.
        free_space(directory, sum(info.file_size for info in infos))
        refs = {item["archive_path"]: (role, item) for role, item in document["components"].items() if "archive_path" in item}
        attachments = {item["path"]: item for item in document["attachments"]}
        staged = {}
        for info in infos:
            if info.filename == "workflow.json":
                continue
            if info.filename in attachments and attachments[info.filename]["size"] != info.file_size:
                raise ValueError("Attachment size does not match workflow metadata")
            destination = directory / info.filename
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest, count = hashlib.sha256(), 0
            with archive.open(info) as incoming, destination.open("xb") as output:
                os.chmod(destination, 0o600)
                while chunk := incoming.read(CHUNK):
                    free_space(directory, len(chunk))
                    output.write(chunk)
                    digest.update(chunk)
                    count += len(chunk)
            if count != info.file_size:
                raise ValueError("Archive member has an invalid size")
            if info.filename in refs:
                validate_safetensors(destination)
                role, reference = refs[info.filename]
                staged[role] = (destination, reference, digest.hexdigest())
            elif destination.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                from PIL import Image
                try:
                    with Image.open(destination) as image:
                        if image.width * image.height > 64 * CHUNK or getattr(image, "n_frames", 1) != 1:
                            raise ValueError("Invalid workflow image dimensions")
                        image.verify()
                except (OSError, ValueError) as exc:
                    raise ValueError("Invalid workflow image attachment") from exc
        # No payload is published until *every* archive member passed validation.
        self.cache.mkdir(parents=True, exist_ok=True)
        if self.cache.stat().st_dev != directory.stat().st_dev:
            free_space(self.cache, sum(path.stat().st_size for path, _, _ in staged.values()))
        weights = {}
        with _locked(self.cache / ".workflow-import.lock"):
            for role, (path, reference, digest) in staged.items():
                repo = reference.get("repository", "odysseus-imports/" + self.family)
                relative = reference.get("relative_path", reference["name"])
                # A local content-addressed revision cannot impersonate or replace
                # an existing upstream commit. Download refs stay in workflow.json.
                target = self.cache / ("models--" + repo.replace("/", "--")) / "snapshots" / ("odysseus-" + digest[:32]) / relative
                if not target.resolve().is_relative_to(self.cache.resolve()):
                    raise ValueError("Model cache path escapes the private environment")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    if not target.is_file() or file_hash(target) != digest:
                        raise ValueError("An existing cached weight differs; it was not overwritten")
                else:
                    try:
                        os.link(path, target)
                    except FileExistsError:
                        if file_hash(target) != digest:
                            raise ValueError("An existing cached weight differs; it was not overwritten")
                    except OSError as exc:
                        if exc.errno != 18:
                            raise
                        free_space(target.parent, path.stat().st_size)
                        temporary = target.with_name("." + uuid.uuid4().hex + ".partial")
                        try:
                            shutil.copyfile(path, temporary)
                            os.chmod(temporary, 0o600)
                            os.link(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
                weights[role] = str(target)
                original = {key: value for key, value in reference.items() if key != "archive_path"}
                # Preserve the first source identity for identical imported
                # bytes. Neither existing weights nor their provenance changes.
                try:
                    with provenance_path(target).open("x", encoding="utf-8") as metadata:
                        os.chmod(metadata.name, 0o600)
                        json.dump({"sha256": digest, "reference": original}, metadata)
                except FileExistsError:
                    pass
                path.unlink()
        source.unlink(missing_ok=True)
        return self._finish_import(directory, owner, document, weights)
