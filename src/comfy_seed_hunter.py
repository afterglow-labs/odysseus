"""Verified Seed Hunter v2.5.1 graph adapter to its existing native recipe.

Topology is checked independently of editable controls. This prevents treating
a similarly named but modified processing graph as the shipped native recipe.
Imported calculator expressions are compared to known formulas, never evaluated.
"""
import hashlib
import json
import math

TOPOLOGY = "f849b8558709812f42087ad91acb21f113305c21b926c930f04690d748fa04cd"
MODEL_GROUP = "fc72fed4-8bc4-4873-9164-c09cc633cce6"
SETTINGS_GROUP = "b240b117-e18a-4bbf-bc5e-580a3284e8ac"
FORMULAS = {24: "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17",
            126: "a", 127: "a + 1000", 137: "a + 2000"}


def topology(graph, depth=0, budget=None):
    if budget is None:
        budget = [600, 3000]
    if depth > 8 or not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("links", []), list):
        raise ValueError("Invalid or excessively nested Seed Hunter graph")
    budget[0] -= len(graph["nodes"])
    budget[1] -= len(graph.get("links", []))
    if min(budget) < 0:
        raise ValueError("Workflow is too large")
    for node in graph["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("inputs", []), list) or any(
                not isinstance(port, dict) or not isinstance(port.get("name"), str) for port in node.get("inputs", [])):
            raise ValueError("Invalid Seed Hunter node or input socket")
    definitions = graph.get("definitions", {})
    if not isinstance(definitions, dict) or not isinstance(definitions.get("subgraphs", []), list):
        raise ValueError("Invalid Seed Hunter subgraphs")
    def interface(key):
        ports = graph.get(key, [])
        if not isinstance(ports, list) or any(not isinstance(port, dict) for port in ports):
            raise ValueError("Invalid Seed Hunter interface")
        return [{field: port.get(field) for field in ("id", "name", "type", "linkIds")} for port in ports]
    def wiring(node):
        props = node.get("properties", {})
        if not isinstance(props, dict) or not isinstance(props.get("ue_properties", {}), dict):
            raise ValueError("Invalid implicit wiring settings")
        ue = props.get("ue_properties", {})
        # Version/UI state does not affect execution. Socket exclusions and
        # broadcaster restrictions do, including otherwise invisible links.
        fields = ("group_restricted", "color_restricted", "title_regex", "input_regex", "group_regex",
                  "title_regex_invert", "input_regex_invert", "group_regex_invert", "repeated_type_rule",
                  "apply_to_unrepeated", "string_to_combo", "send_to_any")
        return {"input_ue_unconnectable": ue.get("input_ue_unconnectable", {}),
                "broadcast": {key: ue.get(key) for key in fields} if node.get("type") == "Anything Everywhere" else {}}
    return {"nodes": [{key: node.get(key) for key in ("id", "type", "mode")} |
                      {"inputs": [(port["name"], port.get("link")) for port in node.get("inputs", [])],
                       "wiring": wiring(node)}
                      for node in graph["nodes"]], "links": graph.get("links", []),
            "inputs": interface("inputs"), "outputs": interface("outputs"),
            "subgraphs": [topology(child, depth + 1, budget) for child in definitions.get("subgraphs", [])]}


def convert_known_seed_hunter(document, *, output_node=None):
    from src.comfy_workflow_conversion import ConversionError, filename, issue, _settings
    from src.video_workflow import FORMAT, VERSION, INPUT_LAYOUT
    from scripts.h3_seed_hunter_controls import validate_options

    if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
        return None
    if not any(isinstance(node, dict) and node.get("type") == MODEL_GROUP for node in document["nodes"]):
        return None
    # Validate before exposing a graph preview or assuming UI node structure.
    shape = topology(document)
    result = {"format": "odysseus-comfyui-conversion", "version": 1, "convertible": False, "runnable": False,
              "family": "h3", "document": None, "config": None, "inputs": [], "issues": [],
              "outputs": [{"id": "seed_hunter_preview", "type": "Seed Hunter · three previews"}],
              "graph": {"nodes": [{"id": str(n.get("id")), "type": n.get("type"), "active": n.get("mode", 0) == 0,
                                    "mode": n.get("mode", 0)} for n in document["nodes"]],
                        "edges": [{"from": str(link[1]), "to": str(link[3]), "output": link[2], "input": link[4]}
                                  for link in document.get("links", []) if isinstance(link, list) and len(link) >= 6]}}
    try:
        if len(document["nodes"]) > 600 or len(document.get("links", [])) > 3000:
            raise ValueError("Workflow is too large")
        actual = hashlib.sha256(json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if actual != TOPOLOGY:
            raise ConversionError("seed_hunter_topology", "This Seed Hunter graph has changed processing or enabled another branch. Its changes need a native mapping before it can run.")
        if output_node not in (None, "seed_hunter_preview"):
            raise ValueError("Choose the Seed Hunter preview recipe output")
        nodes = {node["id"]: node for node in document["nodes"]}
        nested = {node["id"]: node for group in document["definitions"]["subgraphs"] for node in group["nodes"]}
        def widgets(identifier, count=None, *, inner=False):
            node = (nested if inner else nodes)[identifier]
            values = node.get("widgets_values")
            if not isinstance(values, list) or count is not None and len(values) != count:
                raise ConversionError("seed_hunter_widgets", "Seed Hunter control layout changed; update its native mapping", {"id": str(identifier), "type": node["type"]})
            return values
        for identifier, formula in FORMULAS.items():
            if widgets(identifier, 1, inner=True)[0] != formula:
                raise ConversionError("calculator", "An imported calculator formula differs from the native recipe; expressions are never executed")
        if (widgets(3, inner=True)[1:] != ["minimax", "default"]
                or widgets(4, inner=True)[1:] != ["default"]
                or widgets(7, inner=True)[2:] != [1]
                or widgets(9, inner=True)[2:] != [32]):
            raise ConversionError("loader_settings", "A hidden loader, scheduler or resolution setting differs from the native recipe")
        sampler, scheduler, steps, megapixels, aspect, seconds = widgets(22, 6)
        if not isinstance(aspect, str) or aspect.split(" ", 1)[0] not in {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"}:
            raise ConversionError("resolution", "Choose a supported aspect ratio")
        if type(megapixels) not in (int, float) or not .1 <= megapixels <= 1 or type(seconds) not in (int, float) or not 0 < seconds <= 15.1:
            raise ConversionError("resolution", "Imported preview size or duration exceeds native limits")
        rw, rh = map(int, aspect.split(" ", 1)[0].split(":"))
        scale = math.sqrt(megapixels * 1024 * 1024 / (rw * rh))
        frames = max(5, round(seconds * 24))
        frames += (5 - frames) % 17
        prompt = widgets(365, 1)[0]
        options = {"stage": "preview", "preview_count": 3, "shift_enabled": False,
                   "attention_backend": widgets(196, 1)[0]}
        sparse = widgets(402, 9)
        if sparse[0] not in {"sla", "vsa"} or sparse[4] != "" or sparse[8] is not False:
            raise ConversionError("sparse_attention", "This sparse attention selection needs an additional native mapping")
        options.update(sparse_method=sparse[0], sparse_keep_percent=sparse[1], sparse_start=sparse[2],
                       sparse_end=sparse[3], sparse_min_tokens=sparse[5], sparse_extra_tokens=sparse[6],
                       sparse_sink_conditioning="none" if sparse[7] == "off" else sparse[7])
        options = validate_options(options)
        media_values = widgets(416, 4)
        media = json.loads(media_values[0])
        if media:
            raise ConversionError("seed_hunter_media", "This saved media-loader selection needs an explicit native slot mapping; no source paths were opened")
        config = {"recipe": "seed_hunter", "seed_hunter": options, "mode": "ref2va", "prompt": prompt,
                  "sampler": sampler, "scheduler": scheduler, "steps": steps, "seed": widgets(16)[0],
                  "width": round(rw * scale / 32) * 32, "height": round(rh * scale / 32) * 32,
                  "frames": frames, "reference_size": widgets(5, 5)[4]}
        names = widgets(171, 4)
        components = {role: {"name": filename(value)} for role, value in zip(("model", "encoder", "video_vae", "audio_vae"), names)}
        state = widgets(554, 1)[0]
        if not isinstance(state, dict) or not isinstance(state.get("loras"), list):
            raise ConversionError("lora_stack", "The LoRA stack is missing or invalid")
        if any(not isinstance(row, dict) or type(row.get("on")) is not bool for row in state["loras"]):
            raise ConversionError("lora_stack", "Each imported LoRA must have an explicit enabled/disabled selection", {"id": "554", "type": "PixaromaLoraLoader"})
        saved_state = nodes[554].get("properties", {}).get("loraLoaderState")
        if isinstance(saved_state, str) and json.loads(saved_state).get("loras") != state["loras"]:
            raise ConversionError("lora_state", "The graph contains conflicting saved LoRA selections")
        rows = [row for row in state["loras"] if isinstance(row, dict) and row.get("on") is True]
        from src.h3_vfx import VFX_LORAS
        if any(filename(row.get("name")).lower() in VFX_LORAS and row.get("sm") != 0 for row in rows):
            raise ConversionError("vfx_source_guide", "The VFX Edit adapter needs a standard H3 source-guide workflow, not Seed Hunter", {"id": "554", "type": "PixaromaLoraLoader"})
        config["lora_strengths"] = []
        for index, row in enumerate(rows):
            components[f"lora_{index}"] = {"name": filename(row.get("name"))}
            config["lora_strengths"].append(row.get("sm"))
        for identifier in (18, 134, 144):
            values = nodes[identifier].get("widgets_values", {})
            if (not isinstance(values, dict) or values.get("frame_rate") != 24 or values.get("loop_count") != 0
                    or values.get("pingpong") is not False or values.get("trim_to_audio") is not False
                    or values.get("format") != "video/h264-mp4" or values.get("pix_fmt") != "yuv420p"):
                raise ConversionError("output_processing", "A preview output's timing or format differs from the native recipe")
        _settings(config)
        result["document"] = {"format": FORMAT, "version": VERSION, "family": "h3", "input_layout": INPUT_LAYOUT,
                              "config": config, "components": components, "attachments": []}
        result["issues"].extend([
            issue("seed_hunter_recipe", "Converted the three enabled preview branches with seed offsets 0, 1000 and 2000. Bypassed refinement and muted continuation/dialogue branches remain disabled.", severity="warning"),
            issue("output_storage", "Outputs use Odysseus job storage and H.264 CRF 18; ComfyUI output folders and encoder quality widgets are not used.", severity="warning"),
            issue("reference_slots", "The saved media loader is empty. Attach references in the native editor if needed.", severity="warning"),
        ])
    except (ConversionError, KeyError, TypeError, ValueError) as exc:
        result["issues"].append(issue(getattr(exc, "code", "seed_hunter_settings"),
                                     str(exc) if isinstance(exc, (ConversionError, ValueError)) else "Seed Hunter control data is incomplete",
                                     getattr(exc, "node", None)))
    return result
