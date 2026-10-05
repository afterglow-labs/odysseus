"""Read cached model artifacts without importing ML packages or loading tensors.

Kept standalone: Cookbook embeds this source in its remote cache scanner.
"""

import json
import os
import re
import struct


def artifact_family(text):
    value = text.lower().replace("_", "-")
    for pattern, family in (
        (r"ltx-?2[.-]?5", "LTX-2.5"), (r"ltx-?2[.-]?3", "LTX-2.3"),
        (r"ltx-?2", "LTX-2"), (r"wan-?2[.-]?2|bernini", "Wan 2.2"),
        (r"minimax.*h3|(?:^|/)h3(?:/|$)", "MiniMax H3"),
    ):
        if re.search(pattern, value):
            return family
    return ""


def _safetensors_kind(path):
    """Only read the bounded JSON header; never deserialize tensor payloads."""
    try:
        with open(path, "rb") as stream:
            length = struct.unpack("<Q", stream.read(8))[0]
            if not 0 < length <= 8 * 1024 * 1024:
                return False, False
            header = json.loads(stream.read(length))
        keys = [key for key in header if key != "__metadata__"]
        adapter = any(re.search(r"(?:lora_[ab]|lora_(?:up|down))(?:\.[^.]+)?\.weight$", key, re.I) for key in keys)
        diffusion = any(key.startswith(("diffusion_model.", "lora_unet_", "unet.", "transformer.")) for key in keys)
        return adapter, diffusion
    except (OSError, ValueError, TypeError, struct.error):
        return False, False


def _base_models(directory):
    models = []
    try:
        with open(os.path.join(directory, "adapter_config.json"), encoding="utf-8") as stream:
            model = json.load(stream).get("base_model_name_or_path")
        if isinstance(model, str) and model:
            models.append(model)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(directory, "README.md"), encoding="utf-8") as stream:
            text = stream.read(65536)
        front = text.split("---", 2)[1] if text.startswith("---") else ""
        match = re.search(r"(?m)^base_model:[ \t]*([^\n]*)(\n(?:[ \t]*-[^\n]*\n?)*)", front)
        if match:
            # HF's common scalar/list frontmatter, without a YAML dependency
            # on remote hosts. Only accept repo identifiers, never YAML tags.
            values = [match[1].strip()] + [line.strip().lstrip("- ") for line in match[2].splitlines()]
            for value in values:
                value = value.strip(" \"'")
                if re.fullmatch(r"[\w.-]+/[\w.-]+", value) and value not in models:
                    models.append(value)
    except (OSError, ValueError):
        pass
    return models


def _workflow_family(path):
    family = artifact_family(path)
    if family and family != "LTX-2":
        return family
    try:
        if os.path.getsize(path) > 4 * 1024 * 1024:
            return family
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        # Some v3 workflow filenames still say ltx2; use their selected
        # transformer/UNet to distinguish LTX-2.3 from the older base.
        def nodes(value):
            if isinstance(value, dict):
                if "type" in value or "class_type" in value:
                    yield value
                for item in value.values():
                    yield from nodes(item)
            elif isinstance(value, list):
                for item in value:
                    yield from nodes(item)
        for node in nodes(data):
            kind = str(node.get("type") or node.get("class_type") or "").lower()
            if any(name in kind for name in ("unetloader", "checkpointloader", "diffusionmodel")):
                inferred = artifact_family(json.dumps(node.get("widgets_values") or node.get("inputs") or {}))
                if inferred:
                    return inferred
    except (OSError, ValueError):
        pass
    return family


def scan_model_artifacts(directory):
    """Return file-level adapter/workflow metadata for one snapshot or folder."""
    adapters, workflows = [], []
    base_models = _base_models(directory)
    has_base = os.path.isfile(os.path.join(directory, "model_index.json")) or os.path.isfile(os.path.join(directory, "config.json"))
    has_other_weights = False
    diffusion = False
    for root, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not os.path.islink(os.path.join(root, d))]
        for name in sorted(names):
            if name.startswith("._"):
                continue
            path = os.path.join(root, name)
            relative = os.path.relpath(path, directory).replace(os.sep, "/")
            lower = name.lower()
            if lower.endswith(".json") and ("workflow" in lower or "workflows" in relative.split("/")):
                workflows.append({"name": name, "rel_path": relative, "family": _workflow_family(path)})
            if not lower.endswith(".safetensors"):
                if lower.endswith((".bin", ".gguf")) and "mmproj" not in lower:
                    has_other_weights = True
                continue
            adapter, is_diffusion = _safetensors_kind(path)
            adapter = adapter or lower in {"adapter_model.safetensors", "pytorch_lora_weights.safetensors"} or "lora" in lower
            if not adapter:
                has_other_weights = True
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            family = artifact_family(relative)
            compatible = [model for model in base_models if not family or artifact_family(model) == family]
            if not family and len(base_models) == 1:
                family = artifact_family(base_models[0])
            adapters.append({"name": name, "rel_path": relative, "size_bytes": size,
                             "role": "adapter", "family": family, "base_models": compatible})
            diffusion = diffusion or is_diffusion or bool(family)
    return {"adapter_files": sorted(adapters, key=lambda f: f["rel_path"]),
            "workflow_files": sorted(workflows, key=lambda f: f["rel_path"]),
            "base_models": base_models, "is_adapter": bool(adapters),
            "has_base_model": has_base or has_other_weights,
            "adapter_only": bool(adapters) and not has_base and not has_other_weights,
            "is_diffusion": diffusion,
            "is_video": any(f["family"] for f in adapters)}
