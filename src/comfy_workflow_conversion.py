"""Deterministic data-only conversion of Comfy graphs to native video recipes.

No imported node class, expression, path or Python module is executed. Each
accepted processing path has an explicit native equivalent; other paths remain
visible in the report for implementing the next adapter.
"""
import copy
import math
from pathlib import PurePosixPath

from src.video_workflow import FORMAT, VERSION, INPUT_LAYOUT, validate_document, compatible

MAX_NODES = 600
MAX_LINKS = 3000
WIDGETS = {
    "UNETLoader": ("unet_name", "weight_dtype"),
    "CLIPLoader": ("clip_name", "type", "device"), "VAELoader": ("vae_name",),
    "LoraLoaderModelOnly": ("lora_name", "strength_model"),
    "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
    "MiniMaxH3ImageToVideo": ("prompt", "width", "height", "length"),
    "MiniMaxH3ReferenceToVideo": ("prompt", "width", "height", "length", "ref_image_size"),
    "MiniMaxH3SigmaShift": ("shift_video", "shift_audio"),
    "MiniMaxH3AddGuide": ("frame_idx",),
    "KSampler": ("seed", "control_after_generate", "steps", "cfg", "sampler_name", "scheduler", "denoise"),
    "BasicScheduler": ("scheduler", "steps", "denoise"), "KSamplerSelect": ("sampler_name",),
    "RandomNoise": ("noise_seed", "control_after_generate"),
    "PrimitiveInt": ("value", "control_after_generate"), "PrimitiveFloat": ("value",),
    "PrimitiveBoolean": ("value",), "PrimitiveString": ("value",), "INTConstant": ("value",),
    "LoadImage": ("image", "upload"), "LoadAudio": ("audio", "upload"),
    "LoadVideo": ("file", "upload"), "CreateVideo": ("fps", "bit_depth", "color_space", "codec"),
    "SaveVideo": ("filename_prefix", "format", "codec"),
    "VAEDecode": (), "VAEDecodeAudio": (), "ConditioningZeroOut": (),
    "BasicGuider": (), "SamplerCustomAdvanced": (), "GetVideoComponents": (),
    "ModelAttentionBackend": ("attention_backend",),
    "MiniMaxChunkFeedForward": ("chunk_size", "dim_threshold"),
    "MiniMaxLowVRAMAttention": ("chunks",),
}
PORTS = {
    "UNETLoader": (), "CLIPLoader": (), "VAELoader": (),
    "LoraLoaderModelOnly": ("model",), "LoraLoader": ("model", "clip"),
    "MiniMaxH3ImageToVideo": ("clip", "vae", "first_frame", "last_frame"),
    "MiniMaxH3ReferenceToVideo": ("clip", "vae", "audio_vae", "ref_images", "ref_videos", "ref_video_audios", "ref_audios"),
    "MiniMaxH3SigmaShift": ("model",), "MiniMaxH3AddGuide": ("positive", "latent", "vae", "audio_vae", "image", "audio"),
    "KSampler": ("model", "positive", "negative", "latent_image"),
    "BasicScheduler": ("model",), "BasicGuider": ("model", "conditioning"),
    "SamplerCustomAdvanced": ("noise", "guider", "sampler", "sigmas", "latent_image"),
    "VAEDecode": ("samples", "vae"), "VAEDecodeAudio": ("samples", "vae"),
    "ConditioningZeroOut": ("conditioning",), "GetVideoComponents": ("video",),
    "CreateVideo": ("images", "audio"), "SaveVideo": ("video",),
    "ModelAttentionBackend": ("model",), "MiniMaxChunkFeedForward": ("model",),
    "MiniMaxLowVRAMAttention": ("model",),
}
OUTPUTS = {"SaveVideo", "VHS_VideoCombine"}
UI_ONLY = {"Note", "MarkdownNote", "PixaromaLabel", "PixaromaGroupSwitch", "PreviewAny"}


class ConversionError(ValueError):
    def __init__(self, code, message, node=None):
        super().__init__(message)
        self.code, self.node = code, node


class ComfyConversionFailure(ValueError):
    def __init__(self, report):
        self.report = report
        messages = [item["message"] for item in report["issues"] if item["severity"] == "error"]
        super().__init__("ComfyUI conversion needs attention: " + "; ".join(messages[:5]))


def issue(code, message, node=None, severity="error"):
    return {"severity": severity, "code": code, "node_id": node.get("id") if node else None,
            "node_type": node.get("type") if node else None, "message": message}


def filename(value):
    # Imported paths are labels only; do not resolve, stat, open or download them.
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ConversionError("invalid_filename", "A component or media filename is missing or invalid")
    name = PurePosixPath(value.replace("\\", "/")).name
    if name in {"", ".", ".."} or ":" in name or len(name) > 240:
        raise ConversionError("invalid_filename", "Choose a valid component or media filename")
    return name


def reference(value):
    return (isinstance(value, list) and len(value) == 2 and type(value[0]) in (str, int)
            and type(value[1]) is int and value[1] >= 0)


class Graph:
    def __init__(self, document):
        if not isinstance(document, dict):
            raise ValueError("A ComfyUI workflow must be a JSON object")
        self.nodes, self.edges, self.issues = {}, [], []
        raw = document.get("prompt", document)
        if "nodes" in document:
            rows, links = document["nodes"], document.get("links", [])
            if not isinstance(rows, list) or not isinstance(links, list) or len(links) > MAX_LINKS:
                raise ValueError("Invalid or excessive ComfyUI graph links")
            indexed = {}
            for link in links:
                if isinstance(link, list) and len(link) >= 6:
                    indexed[str(link[0])] = link
                else:
                    raise ValueError("Unsupported ComfyUI link encoding")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Invalid ComfyUI node")
                kind = row.get("type")
                values = row.get("widgets_values") or []
                schema = WIDGETS.get(kind)
                inputs = {}
                if schema is not None:
                    if isinstance(values, list) and len(values) <= len(schema):
                        inputs.update(zip(schema, values))
                    elif isinstance(values, dict):
                        inputs.update(values)
                    else:
                        self.issues.append(issue("widget_schema", "This node's widget layout needs an adapter update", {"id": str(row.get("id")), "type": kind}))
                for slot, port in enumerate(row.get("inputs", [])):
                    if not isinstance(port, dict) or not isinstance(port.get("name"), str):
                        raise ValueError("Invalid ComfyUI input socket")
                    if port.get("link") is not None:
                        link = indexed.get(str(port["link"]))
                        if link is None or str(link[3]) != str(row.get("id")) or link[4] != slot:
                            raise ValueError("ComfyUI input references a missing or inconsistent link")
                        inputs[port["name"]] = [str(link[1]), link[2]]
                self._add(row.get("id"), kind, inputs, row.get("mode", 0))
        elif isinstance(raw, dict):
            for identifier, row in raw.items():
                if not isinstance(row, dict) or not isinstance(row.get("inputs", {}), dict):
                    raise ValueError("Use a ComfyUI API prompt or exported UI workflow")
                self._add(identifier, row.get("class_type"), row.get("inputs", {}), 0)
        else:
            raise ValueError("Use a ComfyUI API prompt or exported UI workflow")
        if not self.nodes or len(self.nodes) > MAX_NODES:
            raise ValueError("Workflow must contain between 1 and 600 nodes")
        for node in self.nodes.values():
            for key, value in node["inputs"].items():
                for ref in self.refs(value):
                    if str(ref[0]) not in self.nodes:
                        raise ValueError("Workflow references a missing node")
                    self.edges.append({"from": str(ref[0]), "to": node["id"], "output": ref[1], "input": key})
        if len(self.edges) > MAX_LINKS:
            raise ValueError("Too many workflow connections")

    def _add(self, identifier, kind, inputs, mode):
        if type(identifier) not in (str, int) or not isinstance(kind, str) or not 0 < len(kind) <= 160:
            raise ValueError("Invalid ComfyUI node identity or type")
        identifier = str(identifier)
        if not identifier or len(identifier) > 128 or identifier in self.nodes:
            raise ValueError("Duplicate or invalid ComfyUI node ID")
        self.nodes[identifier] = {"id": identifier, "type": kind, "inputs": inputs, "mode": mode}

    def refs(self, value, depth=0):
        if depth > 32:
            raise ValueError("Workflow inputs are nested too deeply")
        if reference(value):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from self.refs(child, depth + 1)

    def node(self, value, kind=None, slot=0):
        if not reference(value) or value[1] != slot:
            raise ConversionError("connection", "Expected an explicitly connected node output")
        result = self.nodes[str(value[0])]
        if kind and result["type"] not in ({kind} if isinstance(kind, str) else kind):
            raise ConversionError("connection", "This processing connection needs a native adapter", result)
        return result

    def scalar(self, value, depth=0):
        if depth > 32:
            raise ConversionError("cycle", "Workflow contains a cycle or excessive chain")
        if reference(value):
            node = self.node(value, {"PrimitiveInt", "PrimitiveFloat", "PrimitiveBoolean", "PrimitiveString", "INTConstant"})
            return self.scalar(node["inputs"].get("value"), depth + 1)
        return value

    def active(self, identifier):
        seen, visiting = set(), set()
        def walk(key):
            if key in visiting:
                raise ConversionError("cycle", "Workflow contains a processing cycle", self.nodes[key])
            if key in seen:
                return
            if len(visiting) > 100:
                raise ConversionError("depth", "Workflow chain exceeds the supported depth")
            visiting.add(key)
            node = self.nodes[key]
            for value in node["inputs"].values():
                for ref in self.refs(value):
                    walk(str(ref[0]))
            visiting.remove(key)
            seen.add(key)
        walk(identifier)
        return seen

    def public(self, active=()):
        return {"nodes": [{"id": row["id"], "type": row["type"], "active": row["id"] in active,
                           "mode": row["mode"]} for row in self.nodes.values()], "edges": self.edges}


def _settings(config):
    hunter = config.get("recipe") == "seed_hunter"
    for key, low, high in (("width", 256, 4096 if hunter else 1920), ("height", 256, 4096 if hunter else 1920),
                           ("frames", 73 if hunter else 124, 362), ("steps", 1, 100), ("seed", 0, 2**53 - 2001)):
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ConversionError("setting_range", f"Imported {key} is outside this native workflow's supported range")
    if config["width"] % 32 or config["height"] % 32 or (config["frames"] - 5) % 17:
        raise ConversionError("setting_grid", "Dimensions must be multiples of 32 and frames must follow the 17n+5 grid")
    if config["width"] * config["height"] > (1048576 if hunter else 768 * 1344):
        raise ConversionError("setting_range", "Imported resolution exceeds this native workflow's pixel budget")
    if not isinstance(config.get("prompt"), str) or not config["prompt"].strip() or len(config["prompt"]) > 16000:
        raise ConversionError("prompt", "Supply an imported prompt of 1–16,000 characters")
    samplers = {"euler", "res_multistep", *( ["er_sde"] if hunter else [])}
    schedulers = {"simple", "normal", *( ["beta", "linear_quadratic"] if hunter else [])}
    if config.get("sampler") not in samplers or config.get("scheduler") not in schedulers:
        raise ConversionError("sampler", "This sampler or scheduler needs a native implementation")
    if config.get("reference_size") not in {"match", "max"}:
        raise ConversionError("reference_size", "This reference sizing mode needs a native implementation")
    for key in ("shift_video", "shift_audio"):
        if key in config and (type(config[key]) not in (int, float) or not math.isfinite(config[key]) or not .01 <= config[key] <= 100):
            raise ConversionError("sigma_shift", "Sigma shifts must be finite numbers between 0.01 and 100")


def _native(graph, output):
    config, components, inputs, warnings = {}, {}, [], []
    def scalar(node, key, default=None):
        return graph.scalar(node["inputs"].get(key, default))
    def asset(value, role, kind, key):
        node = graph.node(value, kind)
        name = filename(scalar(node, key))
        if role in components and components[role]["name"] != name:
            raise ConversionError("component_conflict", f"Different {role} components are used on the selected path", node)
        components[role] = {"name": name}
        if kind == "CLIPLoader" and (scalar(node, "type", "minimax") != "minimax" or scalar(node, "device", "default") != "default"):
            raise ConversionError("encoder_options", "This encoder type or device override needs a native adapter", node)
    end = graph.nodes[output]
    if end["type"] != "SaveVideo":
        raise ConversionError("output_adapter", "This video output node needs a native output adapter", end)
    fmt, codec = scalar(end, "format", "auto"), scalar(end, "codec", "auto")
    if isinstance(fmt, dict):
        if set(fmt) - {"format", "codec"}:
            raise ConversionError("output_format", "This output container option needs a native adapter", end)
        codec, fmt = fmt.get("codec", codec), fmt.get("format")
    if isinstance(codec, dict):
        if set(codec) - {"codec", "encoding"}:
            raise ConversionError("output_format", "This output codec option needs a native adapter", end)
        encoding = codec.get("encoding", {}) or {}
        if not isinstance(encoding, dict) or set(encoding) - {"encoding", "crf"} or encoding.get("encoding", "auto") not in {"auto", "re-encode"}:
            raise ConversionError("output_format", "This video encoding operation needs a native adapter", end)
        if encoding.get("crf", 18) != 18:
            warnings.append(issue("output_quality", "Odysseus encodes the converted output at H.264 CRF 18 instead of the imported output quality setting", end, "warning"))
        codec = codec.get("codec")
    if fmt not in {"auto", "mp4"} or codec not in {"auto", "h264"}:
        raise ConversionError("output_format", "Native video export currently requires MP4/H.264", end)
    video = graph.node(end["inputs"].get("video"), "CreateVideo")
    if scalar(video, "fps", 30) != 24 or scalar(video, "color_space", "sRGB") != "sRGB" or scalar(video, "bit_depth", "auto") not in {"auto", 8}:
        raise ConversionError("output_format", "This frame rate, colorspace or bit depth needs a native output adapter", video)
    if scalar(video, "codec", "none") != "none":
        raise ConversionError("output_format", "Intermediate video encoding is not supported", video)
    warnings.append(issue("output_storage", "Converted videos are saved in Odysseus job storage; imported output-directory names are not used", end, "warning"))
    decoded = graph.node(video["inputs"].get("images"), "VAEDecode")
    audio = graph.node(video["inputs"].get("audio"), "VAEDecodeAudio")
    sample_ref = decoded["inputs"].get("samples")
    if audio["inputs"].get("samples") != sample_ref:
        raise ConversionError("sample_conflict", "Video and audio must come from the same sampler output", audio)
    sampler = graph.node(sample_ref, {"KSampler", "SamplerCustomAdvanced"})
    asset(decoded["inputs"].get("vae"), "video_vae", "VAELoader", "vae_name")
    asset(audio["inputs"].get("vae"), "audio_vae", "VAELoader", "vae_name")
    if sampler["type"] == "KSampler":
        if scalar(sampler, "cfg", 8) != 1 or scalar(sampler, "denoise", 1) != 1:
            raise ConversionError("sampling_semantics", "Native H3 requires CFG 1 and full initial denoising", sampler)
        model_ref, positive_ref = sampler["inputs"].get("model"), sampler["inputs"].get("positive")
        config.update(seed=scalar(sampler, "seed"), steps=scalar(sampler, "steps"),
                      sampler=scalar(sampler, "sampler_name"), scheduler=scalar(sampler, "scheduler"))
        negative = sampler["inputs"].get("negative")
        if negative is not None:
            zero = graph.node(negative, "ConditioningZeroOut")
            if zero["inputs"].get("conditioning") != positive_ref:
                raise ConversionError("negative_conditioning", "Independent negative conditioning needs a native adapter", zero)
    else:
        guider = graph.node(sampler["inputs"].get("guider"), "BasicGuider")
        noise = graph.node(sampler["inputs"].get("noise"), "RandomNoise")
        algorithm = graph.node(sampler["inputs"].get("sampler"), "KSamplerSelect")
        schedule = graph.node(sampler["inputs"].get("sigmas"), "BasicScheduler")
        model_ref, positive_ref = guider["inputs"].get("model"), guider["inputs"].get("conditioning")
        if schedule["inputs"].get("model") != model_ref or scalar(schedule, "denoise", 1) != 1:
            raise ConversionError("sampling_semantics", "Independent scheduler models or partial denoising need a native adapter", schedule)
        config.update(seed=scalar(noise, "noise_seed"), steps=scalar(schedule, "steps"),
                      sampler=scalar(algorithm, "sampler_name"), scheduler=scalar(schedule, "scheduler"))
    condition = graph.node(positive_ref, {"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"})
    if sampler["inputs"].get("latent_image") != [condition["id"], 1]:
        # JSON API keys can be integer IDs; normalize those links explicitly.
        latent = sampler["inputs"].get("latent_image")
        if not reference(latent) or str(latent[0]) != condition["id"] or latent[1] != 1:
            raise ConversionError("latent", "Sampling must use this conditioner's own empty AV latent", sampler)
    config.update(prompt=scalar(condition, "prompt"), width=scalar(condition, "width"),
                  height=scalar(condition, "height"), frames=scalar(condition, "length"),
                  reference_size=scalar(condition, "ref_image_size", "match"))
    asset(condition["inputs"].get("clip"), "encoder", "CLIPLoader", "clip_name")
    asset(condition["inputs"].get("vae"), "video_vae", "VAELoader", "vae_name")
    if condition["type"] == "MiniMaxH3ReferenceToVideo":
        asset(condition["inputs"].get("audio_vae"), "audio_vae", "VAELoader", "vae_name")
        config["mode"] = "ref2va"
    else:
        config["mode"] = "fl2va" if any(condition["inputs"].get(k) for k in ("first_frame", "last_frame")) else "t2va"
    def media(value, field):
        node = graph.node(value, "LoadImage" if field in {"first_frame", "last_frame", "reference_images"} else "LoadAudio")
        key = "image" if node["type"] == "LoadImage" else "audio"
        inputs.append({"field": field, "name": filename(scalar(node, key)), "node_id": node["id"], "required": True})
    for key in ("first_frame", "last_frame"):
        if key in condition["inputs"]:
            media(condition["inputs"][key], key)
    def slots(prefix):
        values = condition["inputs"].get(prefix, {})
        if not isinstance(values, dict):
            raise ConversionError("media_layout", "Reference slots must be explicitly named", condition)
        values = {**values, **{key.split(".", 1)[1]: value for key, value in condition["inputs"].items() if key.startswith(prefix + ".")}}
        return values
    for prefix, field in (("ref_images", "reference_images"), ("ref_audios", "reference_audio")):
        for value in slots(prefix).values():
            if value is not None:
                media(value, field)
    videos, audios = slots("ref_videos"), slots("ref_video_audios")
    used_audio = set()
    for slot, value in videos.items():
        if value is None:
            continue
        split = graph.node(value, "GetVideoComponents")
        audio_slot = slot.replace("ref_video_", "ref_video_audio_")
        companion = audios.get(audio_slot)
        if not reference(companion) or str(companion[0]) != split["id"] or companion[1] != 1:
            raise ConversionError("video_reference_audio", "Connect each reference video's matching audio output; independent or muted video soundtracks need a native option", split)
        used_audio.add(audio_slot)
        load = graph.node(split["inputs"].get("video"), "LoadVideo")
        inputs.append({"field": "reference_videos", "name": filename(scalar(load, "file")),
                       "node_id": load["id"], "required": True})
    if any(value is not None for key, value in audios.items() if key not in used_audio):
        raise ConversionError("video_reference_audio", "An independent video-audio reference needs an explicit native slot mapping", condition)
    if used_audio:
        warnings.append(issue("video_preprocessing", "Reference videos use Odysseus's 24 fps preprocessing, final-frame padding to the H3 grid, and a 2–15 second limit per video. Video and its matching soundtrack remain paired.", severity="warning"))
    if sum(item["field"] == "reference_videos" for item in inputs) > 3 or sum(item["field"] == "reference_audio" for item in inputs) > 3 or len(inputs) > 9:
        raise ConversionError("media_count", "This graph exceeds the native reference slot budget", condition)
    rows, shift = [], False
    while True:
        node = graph.node(model_ref)
        kind = node["type"]
        if kind == "MiniMaxH3SigmaShift":
            if shift:
                raise ConversionError("shift_stack", "Multiple sigma shifts need explicit composition", node)
            config.update(shift_video=scalar(node, "shift_video"), shift_audio=scalar(node, "shift_audio"))
            shift = True
        elif kind in {"LoraLoader", "LoraLoaderModelOnly"}:
            if kind == "LoraLoader" and scalar(node, "strength_clip", 0) != 0:
                raise ConversionError("clip_lora", "Text-encoder LoRA patches need a native adapter", node)
            rows.append((filename(scalar(node, "lora_name")), scalar(node, "strength_model")))
        elif kind == "UNETLoader":
            if scalar(node, "weight_dtype", "default") != "default":
                raise ConversionError("weight_dtype", "Forced weight dtype needs a native loader setting", node)
            components["model"] = {"name": filename(scalar(node, "unet_name"))}
            break
        else:
            raise ConversionError("model_patch", "This model patch needs a native adapter", node)
        model_ref = node["inputs"].get("model")
    rows.reverse()
    from src.h3_vfx import VFX_LORAS
    if any(name.lower() in VFX_LORAS and strength != 0 for name, strength in rows):
        raise ConversionError("vfx_source_guide", "This VFX Edit graph needs an explicit native source-guide mapping before it can run")
    config["lora_strengths"] = [row[1] for row in rows]
    components.update({f"lora_{i}": {"name": name} for i, (name, _) in enumerate(rows)})
    has_visual_reference = any(item["field"] in {"reference_images", "reference_videos"} for item in inputs)
    if config["mode"] == "ref2va" and (not has_visual_reference or not shift or config["sampler"] == "er_sde" or config["scheduler"] in {"beta", "linear_quadratic"}):
        config.update(recipe="seed_hunter", seed_hunter={"stage": "single", "shift_enabled": shift,
                                                        "attention_backend": "pytorch"})
        warnings.append(issue("native_recipe", "This graph uses the native Seed Hunter single-render engine", severity="warning"))
    elif not shift:
        raise ConversionError("sigma_shift", "This graph omits sigma shifting; the selected native engine needs an explicit equivalent")
    _settings(config)
    return {"format": FORMAT, "version": VERSION, "family": "h3", "input_layout": INPUT_LAYOUT,
            "config": config, "components": components, "attachments": []}, inputs, warnings


def bind_document(document, inventory, inputs):
    config, issues, resolved = copy.deepcopy(document["config"]), [], {}
    for role, reference in document["components"].items():
        candidates = [row for row in inventory.get("components", []) if row.get("name") == reference["name"]
                      and compatible(row, role, document["family"], document["config"])
                      and (role != "model" or document["family"] != "h3" or not row.get("variant")
                           or row["variant"] == ("ref2va" if config["mode"] == "ref2va" else "fl2va"))]
        if len(candidates) == 1:
            resolved[role] = candidates[0]["id"]
        else:
            code = "missing_component" if not candidates else "ambiguous_component"
            issues.append(issue(code, f"Choose an installed {role.replace('_', ' ')} for {reference['name']}", severity="warning"))
    if document["family"] == "h3":
        # Empty IDs deliberately clear an old draft selection. Missing assets
        # must never silently inherit a different model from the open editor.
        config.update({key: resolved.get(key, "") for key in document["components"] if not key.startswith("lora_")})
        config["loras"] = [{"id": resolved.get(f"lora_{i}", f"unavailable:lora_{i}:" + document["components"][f"lora_{i}"]["name"]), "strength": value}
                           for i, value in enumerate(config.pop("lora_strengths", []))]
    else:
        config["components"] = resolved
    for item in inputs:
        issues.append(issue("media_required", f"Attach {item['name']} as {item['field'].replace('_', ' ')}", severity="warning"))
    return config, resolved, issues


def _convert_comfyui(document, inventory=None, *, output_node=None):
    from src.comfy_seed_hunter import convert_known_seed_hunter
    inventory = inventory or {}
    special = convert_known_seed_hunter(document, output_node=output_node)
    if special is not None:
        result = special
    else:
        graph = Graph(document)
        outputs = [{"id": node["id"], "type": node["type"]} for node in graph.nodes.values()
                   if node["type"] in OUTPUTS and node["mode"] == 0]
        result = {"format": "odysseus-comfyui-conversion", "version": 1, "convertible": False,
                  "runnable": False, "family": None, "document": None, "config": None, "inputs": [],
                  "issues": [], "outputs": outputs, "graph": graph.public()}
        if output_node is None:
            if len(outputs) != 1:
                result["issues"].append(issue("output_selection", "Choose one video output branch to convert"))
                return result
            output_node = outputs[0]["id"]
        if str(output_node) not in {row["id"] for row in outputs}:
            raise ValueError("Choose an available output node")
        try:
            active = graph.active(str(output_node))
            result["graph"] = graph.public(active)
            result["issues"].extend(item for item in graph.issues if item["node_id"] in active)
            for identifier in sorted(active):
                node = graph.nodes[identifier]
                if node["type"] not in WIDGETS:
                    result["issues"].append(issue("unsupported_node", "This active node needs a native implementation", node))
                elif node["mode"] != 0:
                    result["issues"].append(issue("node_mode", "Export an API prompt to resolve bypassed or muted processing", node))
                allowed = set(WIDGETS.get(node["type"], ())) | set(PORTS.get(node["type"], ()))
                for key in node["inputs"]:
                    if key not in allowed and not (node["type"] == "MiniMaxH3ReferenceToVideo" and key.startswith(("ref_images.", "ref_videos.", "ref_video_audios.", "ref_audios."))):
                        result["issues"].append(issue("unsupported_input", f"Input {key} needs a native mapping", node))
            if any(item["severity"] == "error" for item in result["issues"]):
                return result
            native, inputs, warnings = _native(graph, str(output_node))
            result.update(document=native, family=native["family"], inputs=inputs)
            result["issues"].extend(warnings)
        except ConversionError as exc:
            result["issues"].append(issue(exc.code, str(exc), exc.node))
            return result
        except (TypeError, KeyError, ValueError) as exc:
            result["issues"].append(issue("invalid_settings", "A connected node contains invalid or incomplete settings"))
            return result
    if result.get("document") is None:
        return result
    try:
        validate_document(result["document"], result["family"])
    except ValueError as exc:
        result["issues"].append(issue("invalid_native_settings", str(exc)))
        result.update(document=None, config=None, convertible=False)
        return result
    config, resolved, binding_issues = bind_document(result["document"], inventory, result["inputs"])
    result.update(convertible=True, config=config, resolved_components=resolved)
    result["issues"].extend(binding_issues)
    result["runnable"] = not any(item["severity"] == "error" or item["code"] in {
        "missing_component", "ambiguous_component", "media_required"} for item in result["issues"])
    return result


def convert_comfyui(document, inventory=None, *, output_node=None):
    try:
        return _convert_comfyui(document, inventory, output_node=output_node)
    except (TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise ValueError("Malformed or excessively nested ComfyUI workflow") from exc
