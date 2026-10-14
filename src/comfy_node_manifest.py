"""Discover user-selected Comfy packages outside the web server interpreter.

Metadata is descriptive, not proof a node can execute. The source package is
trusted code selected by an administrator; a separate process is not an OS
sandbox. Discovery never installs dependencies, invokes a node's execution or
validation methods, or exposes captured package stdout/stderr.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
MANIFEST_DIRECTORY = PROJECT / "runtimes/comfy-packages/manifests"
FORMAT = "odysseus-comfy-node-manifest"
MAX_MANIFEST_BYTES = 12 * 1024 * 1024
MAX_SOURCE_FILES = 20000
SOURCE_SUFFIXES = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".txt", ".so", ".pyd", ".dll", ".dylib", ".c", ".cpp", ".h", ".cu", ".cuh"}
SKIP_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}


def _safe_name(value, limit=200):
    return str(value)[:limit]


def _json_safe(value, depth=0):
    """Keep raw schema shape; represent unsupported objects without calling repr."""
    if depth > 30:
        raise ValueError("Schema nesting exceeds 30 levels")
    if value is None or type(value) in (str, bool, int):
        return value
    if isinstance(value, str):  # Comfy type strings sometimes subclass str.
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else {"__nonfinite__": str(value)}
    if isinstance(value, (tuple, list)):
        return [_json_safe(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items() if isinstance(k, (str, int, float, bool))}
    if isinstance(value, (set, frozenset)):
        safe = [_json_safe(v, depth + 1) for v in value]
        return sorted(safe, key=lambda v: json.dumps(v, sort_keys=True))
    return {"__python_type__": f"{type(value).__module__}.{type(value).__qualname__}"}


def source_receipt(package_path):
    path = Path(package_path).expanduser().resolve(strict=True)
    if not (path.is_dir() or path.is_file() and path.suffix == ".py"):
        raise ValueError("Select a Python package directory or .py module")
    if path.is_file():
        root, files = path.parent, [path]
        name = path.stem
    else:
        root, files, name = path, [], path.name
        for directory, children, filenames in os.walk(path):
            children[:] = sorted(child for child in children if child not in SKIP_PARTS)
            for filename in sorted(filenames):
                candidate = Path(directory) / filename
                if candidate.suffix.lower() not in SOURCE_SUFFIXES:
                    continue
                if candidate.is_symlink() and not candidate.resolve().is_relative_to(root):
                    raise ValueError("Package source contains a symlink outside its directory")
                if candidate.is_file():
                    files.append(candidate)
                    if len(files) > MAX_SOURCE_FILES:
                        raise ValueError("Package contains too many source files")
        files.sort()
    rows = []
    for file in files:
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        rows.append({"path": file.relative_to(root).as_posix(), "size": file.stat().st_size, "sha256": digest.hexdigest()})
    if not rows:
        raise ValueError("Package has no discoverable source files")
    fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60] or "package"
    # IDs do not disclose filesystem paths, but separate same-name checkouts.
    identity = slug + "-" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    return {"id": identity, "name": name, "package_path": str(path), "source_path": str(path), "fingerprint": fingerprint, "files": rows}


def verify_source(manifest):
    source = manifest.get("source") or manifest.get("package") or {}
    path, expected = source.get("package_path") or source.get("source_path"), source.get("fingerprint")
    if not path or not expected:
        raise ValueError("This metadata has no installed package source binding")
    actual = source_receipt(path)
    if actual["fingerprint"] != expected:
        raise ValueError("Package source changed after discovery; discover it again before execution")
    return actual


def normalize_node_info(class_type, info, *, package_id=None, function=None, hooks=None):
    """Normalize legacy /object_info (including V3's official v1 projection)."""
    if not isinstance(class_type, str) or not class_type or len(class_type) > 240 or not isinstance(info, dict):
        raise ValueError("Invalid Comfy node metadata")
    raw = _json_safe(info)
    input_types = raw.get("input", raw.get("input_types", {}))
    if not isinstance(input_types, dict):
        raise ValueError("INPUT_TYPES must return an object")
    inputs = []
    order = raw.get("input_order", {})
    for section in ("required", "optional", "hidden"):
        definitions = input_types.get(section, {})
        if not isinstance(definitions, dict):
            raise ValueError("Each input section must contain named inputs")
        names = list(dict.fromkeys([*(order.get(section, []) if isinstance(order, dict) else []), *definitions]))
        for name in names:
            if name not in definitions:
                continue
            definition = definitions[name]
            if isinstance(definition, str):
                kind, metadata = definition, {}
            elif isinstance(definition, list) and definition:
                kind = definition[0]
                metadata = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
            else:
                raise ValueError("Each input needs a type or combo choices")
            combo = isinstance(kind, list)
            type_name = "COMBO" if combo else kind if isinstance(kind, str) else "UNKNOWN"
            force = metadata.get("forceInput", metadata.get("force_input", False)) is True
            item = {"name": name, "type": type_name, "section": section, "required": section == "required",
                    "hidden": section == "hidden", "is_widget": section != "hidden" and not force and (combo or type_name in {"STRING", "INT", "FLOAT", "BOOLEAN"}),
                    "force_input": force, "default_input": metadata.get("defaultInput", metadata.get("default_input", False)) is True,
                    "is_list": raw.get("is_input_list", raw.get("input_is_list", False)) is True,
                    "metadata": metadata, "raw": definition}
            if combo:
                item["choices"] = kind
                if kind:
                    item["default"] = kind[0]
            if "default" in metadata:
                item["default"] = metadata["default"]
            inputs.append(item)
    return_types = raw.get("output", raw.get("return_types", []))
    if not isinstance(return_types, list):
        raise ValueError("RETURN_TYPES must be a sequence")
    names = raw.get("output_name", raw.get("return_names", return_types))
    list_flags = raw.get("output_is_list", [False] * len(return_types))
    if not isinstance(names, list) or not isinstance(list_flags, list):
        raise ValueError("Output names and list flags must be sequences")
    outputs = [{"index": index, "name": str(names[index]) if index < len(names) else str(kind), "type": str(kind),
                "is_list": bool(list_flags[index]) if index < len(list_flags) else False}
               for index, kind in enumerate(return_types)]
    return {"type": "comfy." + class_type, "class_type": class_type, "package_id": package_id,
            "display_name": raw.get("display_name", class_type), "category": raw.get("category", "ComfyUI"),
            "description": raw.get("description", ""), "function": function or raw.get("function"),
            "inputs": inputs, "outputs": outputs, "input_types": input_types,
            "input_is_list": raw.get("is_input_list", raw.get("input_is_list", False)) is True,
            "output_is_list": list_flags, "output_node": raw.get("output_node", False) is True,
            "has_intermediate_output": raw.get("has_intermediate_output", False) is True,
            "deprecated": raw.get("deprecated", False), "experimental": raw.get("experimental", False),
            "hooks": hooks or {"validate_inputs": {"present": None}, "is_changed": {"present": None}},
            "raw_schema": raw, "metadata_status": "ready", "execution_status": "metadata_only"}


def convert_object_info(value):
    if not isinstance(value, dict) or len(value) > 20000:
        raise ValueError("Choose a ComfyUI /object_info JSON object")
    nodes, issues = [], []
    for class_type, info in value.items():
        try:
            node = normalize_node_info(class_type, info)
            node["execution_status"] = "unbound"
            nodes.append(node)
        except (TypeError, ValueError) as error:
            issues.append({"code": "node_schema", "class_type": _safe_name(class_type), "exception_type": type(error).__name__, "message": "This node metadata could not be normalized"})
    return {"format": FORMAT, "version": 1, "status": "partial" if nodes and issues else "ready" if nodes else "failed",
            "execution_status": "unbound", "source": {"kind": "object_info", "fingerprint": hashlib.sha256(json.dumps(_json_safe(value), sort_keys=True).encode()).hexdigest()},
            "nodes": nodes, "issues": issues, "raw_source": _json_safe(value)}


def _failure(receipt, code, message, **details):
    return {"format": FORMAT, "version": 1, "status": "failed", "execution_status": "metadata_only",
            "source": {**receipt, "kind": "python_package"}, "package": receipt, "nodes": [],
            "issues": [{"code": code, "message": message, **details}]}


def discover_node_package(package_path, *, python_path=None, runtime_path=None, timeout=45, model_paths=None):
    receipt = source_receipt(package_path)
    python = Path(python_path or PROJECT / "venv/bin/python").absolute()
    runtime = Path(runtime_path or PROJECT / "runtimes/minimax-h3/ComfyUI").resolve()
    if not python.is_file():
        raise ValueError("The selected private Python interpreter does not exist")
    if not runtime.is_dir():
        raise ValueError("The selected Comfy runtime does not exist")
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise ValueError("Discovery timeout must be between 0 and 300 seconds")
    if model_paths is None:
        model_paths = {}
        base = PROJECT / "models"
        for category in ("checkpoints", "loras", "vae", "text_encoders", "diffusion_models", "vae_approx", "latent_upscale_models"):
            if (base / category).is_dir():
                model_paths[category] = [str(base / category)]
    if not isinstance(model_paths, dict) or len(model_paths) > 100:
        raise ValueError("Model paths must map category names to directories")
    clean_paths = {}
    for category, paths in model_paths.items():
        if not isinstance(category, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", category) or not isinstance(paths, (tuple, list)) or len(paths) > 1000:
            raise ValueError("Invalid model directory category")
        clean_paths[category] = [str(Path(path).expanduser().resolve(strict=True)) for path in paths]
        if any(not Path(path).is_dir() for path in clean_paths[category]):
            raise ValueError("Model paths must be directories")
    script = PROJECT / "scripts/comfy_node_discovery.py"
    with tempfile.TemporaryDirectory(prefix="odysseus-comfy-discovery-") as temp:
        output = Path(temp) / "manifest.json"
        env = {"PATH": str(python.parent) + os.pathsep + "/usr/bin:/bin", "HOME": temp, "TMPDIR": temp,
               "XDG_CACHE_HOME": temp + "/cache", "HF_HOME": temp + "/huggingface", "HF_HUB_OFFLINE": "1",
               "TRANSFORMERS_OFFLINE": "1", "CUDA_VISIBLE_DEVICES": "", "PYTHONNOUSERSITE": "1",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8"}
        config = Path(temp) / "model-paths.json"
        config.write_text(json.dumps(clean_paths))
        command = [str(python), "-I", str(script), "--package", receipt["package_path"], "--runtime", str(runtime), "--output", str(output), "--model-paths", str(config)]
        # stdout/stderr are intentionally discarded, even when the import fails.
        # Packages may print arbitrary environment values or flood their output.
        process = subprocess.Popen(command, env=env, cwd=temp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
            return _failure(receipt, "timeout", "Package discovery exceeded its time limit")
        if process.returncode != 0 or not output.is_file():
            return _failure(receipt, "subprocess_failed", "Package discovery stopped before returning metadata", exit_code=process.returncode)
        if output.stat().st_size > MAX_MANIFEST_BYTES:
            return _failure(receipt, "manifest_size", "Package metadata exceeded the manifest size limit")
        try:
            result = json.loads(output.read_text())
            if result.get("format") != FORMAT or result.get("version") != 1 or not isinstance(result.get("nodes"), list):
                raise ValueError("Invalid manifest")
            if source_receipt(package_path)["fingerprint"] != receipt["fingerprint"]:
                return _failure(receipt, "source_changed", "Package source changed during discovery; try again")
            result["source"] = {**receipt, "kind": "python_package", "runtime_path": str(runtime)}
            result["package"] = receipt
            result["package_id"] = receipt["id"]
            for node in result["nodes"]:
                node["package_id"] = receipt["id"]
            return result
        except (ValueError, OSError, TypeError):
            return _failure(receipt, "invalid_manifest", "Package discovery returned invalid metadata")


def load_manifests(directory=None):
    folder = Path(directory or MANIFEST_DIRECTORY)
    if not folder.is_dir():
        return []
    values = []
    for path in sorted(folder.glob("*.json")):
        if path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
            continue
        try:
            value = json.loads(path.read_text())
            if value.get("format") == FORMAT and value.get("version") == 1 and isinstance(value.get("nodes"), list):
                values.append(value)
        except (OSError, TypeError, ValueError):
            continue
    return values
