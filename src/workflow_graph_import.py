"""ComfyUI and existing native drafts imported as editable graph documents.

Known operations become typed native groups. The complete original JSON,
including disabled nodes, nested subgraphs, widgets and links, is retained under
``source.document``. Unsupported graphs remain visible and blocked, never
silently reduced to one previously hard-coded output.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import PurePosixPath

from src.workflow_graph import (FORMAT, VERSION, COMPONENTS, REGISTRY, GENERATION_FIELDS, HUNTER_FIELDS,
                                make_node, make_edge, validate_document, node_registry)

SEED_HUNTER_SHAPE = "ae5b5409aab621e13ce843809a01dcf5b45112a2829766a1e58632a1dd6006aa"


def _name(value):
    return PurePosixPath(str(value or "").replace("\\", "/")).name


def _component(role, name, inventory=None, selected=""):
    result = {"component": selected or "", "name": _name(name)}
    inventory = (inventory or {}).get("h3", inventory or {})
    if not selected and result["name"]:
        rows = [c for c in inventory.get("components", []) if c.get("role") == role and _name(c.get("name")) == result["name"]]
        if len(rows) == 1:
            result["component"] = rows[0]["id"]
    return result


def from_native_config(config, family="h3", input_counts=None, *, title="Imported workflow", components=None, inventory=None):
    """Decompose native settings into connected operations without queueing them."""
    if not isinstance(config, dict) or family not in {"h3", "bfs"}:
        raise ValueError("Choose a native H3 or BFS draft")
    doc = {"format": FORMAT, "version": VERSION, "title": title, "nodes": [], "edges": []}
    counts = input_counts or {}
    if not isinstance(counts, dict) or any(type(v) is not int or not 0 <= v <= 32 for v in counts.values()):
        raise ValueError("Invalid media slot counts")
    names = components or {}
    def add(identifier, kind, **kwargs):
        doc["nodes"].append(make_node(identifier, kind, **kwargs))
    def wire(a, ap, b, bp):
        doc["edges"].append(make_edge(a, ap, b, bp))
    component_values = config.get("components", {}) if family == "bfs" else config
    for i, role in enumerate(COMPONENTS[:4]):
        add(role, "component." + role, x=40, y=40 + i * 150,
            params=_component(role, names.get(role, {}).get("name"), inventory, component_values.get(role, "")))
    loras = config.get("loras")
    if not isinstance(loras, list):
        loras = []
        if config.get("lora"):
            loras = [{"id": config["lora"], "strength": config.get("lora_scale", 1)}]
        elif family == "bfs":
            for role, strength in (("lora", "lora_scale"), ("speed_lora", "speed_lora_scale")):
                if component_values.get(role):
                    loras.append({"id": component_values[role], "strength": config.get(strength, 1)})
        elif config.get("lora_strengths"):
            loras = [{"id": "", "strength": v} for v in config["lora_strengths"]]
    upstream = "model"
    for i, row in enumerate(loras):
        identifier = f"lora_{i}"
        value = _component("lora", names.get(identifier, {}).get("name"), inventory, row.get("id", ""))
        value.update(strength=row.get("strength", 1), enabled=row.get("enabled", True))
        add(identifier, "model.lora", x=300 + i * 250, y=40, params=value)
        wire(upstream, "model", identifier, "model"); upstream = identifier
    add("setup", "hub.setup", x=620, y=200)
    for role in COMPONENTS[:4]:
        wire(upstream if role == "model" else role, role, "setup", role)
    if config.get("latent_upscaler") or names.get("latent_upscaler"):
        add("latent_upscaler", "component.latent_upscaler", x=40, y=640,
            params=_component("latent_upscaler", names.get("latent_upscaler", {}).get("name"), inventory, config.get("latent_upscaler", "")))
        wire("latent_upscaler", "latent_upscaler", "setup", "latent_upscaler")
    add("prompt", "input.prompt", x=620, y=390, params={"text": config.get("prompt", "")})
    if family == "bfs":
        kind = "bfs.generate"
        params = {k: v for k, v in config.items() if k not in {"components", "prompt", "loras", "lora", "lora_scale"}}
    else:
        options = config.get("seed_hunter", {})
        stage = options.get("stage", "preview")
        kind = "h3.seed_scout" if stage == "scout" else "hunter." + stage if config.get("recipe") == "seed_hunter" else "h3.generate"
        if kind not in REGISTRY or kind == "hunter.final":
            raise ValueError("Open a saved preview for refinement; it cannot be detached from its latent snapshot")
        params = {p["id"]: config[p["id"]] for p in GENERATION_FIELDS if p["id"] in config}
        params.update({k: v for k, v in options.items() if k != "stage"})
        add("references", "input.references", x=620, y=570)
        wire("references", "references", "generate", "references")
    add("generate", kind, x=980, y=300, params=params)
    wire("setup", "setup", "generate", "setup"); wire("prompt", "prompt", "generate", "prompt")
    for index, (slot, count) in enumerate(counts.items()):
        allowed = {p["id"]: p["type"] for p in REGISTRY["input.references"]["inputs"]} if family == "h3" else {"source_video": "VIDEO", "identity_image": "IMAGE", "last_frame": "IMAGE", "mask_video": "VIDEO"}
        if slot not in allowed:
            raise ValueError("Unsupported media slot " + slot)
        for i in range(count):
            identifier = f"{slot}__{i}"
            medium = allowed[slot].lower()
            add(identifier, "input." + medium, x=40 + i * 250, y=850 + index * 150, params={"name": slot.replace("_", " ") + f" {i+1}", "asset": ""})
            wire(identifier, medium, "references" if family == "h3" else "generate", slot)
    add("output", "output.video", x=1300, y=300)
    wire("generate", "result", "output", "result")
    if family == "bfs":
        # BFS families use different slots (Wan low-noise model, LTX connector).
        # Preserve their entire component bundle instead of forcing an H3 hub.
        removed = {n["id"] for n in doc["nodes"] if n["type"].startswith("component.") or n["type"] == "model.lora"}
        doc["nodes"] = [n for n in doc["nodes"] if n["id"] not in removed]
        doc["edges"] = [e for e in doc["edges"] if e["source"]["node"] not in removed and e["target"]["node"] not in removed]
        hub = next(n for n in doc["nodes"] if n["id"] == "setup")
        hub["type"] = "bfs.components"; hub["params"] = copy.deepcopy(config.get("components", {}))
        for e in list(doc["edges"]):
            if e["target"]["node"] == "setup": doc["edges"].remove(e)
        generation = next(n for n in doc["nodes"] if n["id"] == "generate")
        for k in ("lora_scale", "speed_lora_scale"):
            if k in config: generation["params"][k] = config[k]
    return validate_document(doc)


def _without_modes(value):
    if isinstance(value, dict):
        return {k: _without_modes(v) for k, v in value.items() if k != "mode"}
    if isinstance(value, list):
        return [_without_modes(v) for v in value]
    return value


def _known_seed_hunter(source, title, inventory):
    from src.comfy_seed_hunter import MODEL_GROUP, FORMULAS, topology
    if not isinstance(source.get("nodes"), list) or not any(n.get("type") == MODEL_GROUP for n in source["nodes"] if isinstance(n, dict)):
        return None
    shape = hashlib.sha256(json.dumps(_without_modes(topology(source)), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if shape != SEED_HUNTER_SHAPE:
        return None
    nodes = {n["id"]: n for n in source["nodes"]}
    nested = {n["id"]: n for group in source.get("definitions", {}).get("subgraphs", []) for n in group.get("nodes", [])}
    def values(identifier, default=(), inner=False):
        raw = (nested if inner else nodes).get(identifier, {}).get("widgets_values")
        return raw if isinstance(raw, (list, dict)) else default
    for identifier, formula in FORMULAS.items():
        if values(identifier, inner=True) != [formula]:
            return None
    def active(identifier):
        return nodes.get(identifier, {}).get("mode", 0) == 0
    def state(identifier):
        return {0: "active", 2: "disabled", 4: "bypassed"}.get(nodes.get(identifier, {}).get("mode", 0), "disabled")
    def sparse(identifier):
        raw = values(identifier)
        if len(raw) != 9:
            return {}
        return {"sparse_method": raw[0] if active(identifier) else "none", "sparse_keep_percent": raw[1], "sparse_start": raw[2],
                "sparse_end": raw[3], "sparse_min_tokens": raw[5], "sparse_extra_tokens": raw[6],
                "sparse_sink_conditioning": "none" if raw[7] == "off" else raw[7]}
    settings = values(22)
    if len(settings) != 6 or len(values(171)) != 4:
        return None
    sampler, scheduler, steps, megapixels, aspect, seconds = settings
    ratio = str(aspect).split(" ")[0].split(":")
    rw, rh = map(int, ratio)
    scale = math.sqrt(float(megapixels) * 1024 * 1024 / (rw * rh))
    width, height = round(rw * scale / 32) * 32, round(rh * scale / 32) * 32
    frames = max(5, round(float(seconds) * 24)); frames += (5 - frames) % 17
    shifts = values(195, [12, 6])
    common = {"mode": "ref2va", "width": width, "height": height, "frames": frames, "steps": steps,
              "sampler": sampler, "scheduler": scheduler, "seed": values(16, [42])[0], "reference_size": values(5, ["", 0, 0, 0, "match"])[4],
              "shift_video": shifts[0], "shift_audio": shifts[1], "shift_enabled": active(195),
              "attention_backend": values(196, ["comfy kitchen attention"])[0],
              "first_frame_guide": active(272), "last_frame_guide": active(279), "force_audio": active(255),
              "ff_chunk_enabled":active(193), "ff_chunk_size": values(193, [4, 2048])[0], "ff_chunk_threshold": values(193, [4, 2048])[1],
              "low_vram_enabled":active(305), "low_vram_chunks": values(305, [4])[0],
              "output_fps": 24, "interpolation_enabled":active(165), "interpolation_fps":int(values(168,[48])[0]), **sparse(402)}
    # These optional modifiers have no native equivalent yet. Preserve their
    # entire connected graph rather than silently ignoring an enabled change.
    for identifier in (402, 403):
        raw = values(identifier)
        if active(identifier) and (len(raw) != 9 or raw[4] != "" or raw[8] is not False):
            return None
    prompt = values(365, [""])[0]
    config = {**common, "prompt": prompt, "recipe": "seed_hunter", "seed_hunter": {"stage": "preview"}, "lora_strengths": []}
    components = {role: {"name": _name(name)} for role, name in zip(COMPONENTS[:4], values(171))}
    raw_loras = values(554, [{}])[0].get("loras", [])
    for i, row in enumerate(raw_loras):
        components[f"lora_{i}"] = {"name": _name(row.get("name"))}
        config["lora_strengths"].append(row.get("sm", 1))
    doc = from_native_config(config, title=title, components=components, inventory=inventory)
    # All adapter rows survive; disabled ones are real pass-through nodes.
    for i, row in enumerate(raw_loras):
        n = next(n for n in doc["nodes"] if n["id"] == f"lora_{i}")
        n["params"]["enabled"] = bool(row.get("on", True)) and active(554)
        n["source_nodes"] = ["554"]
    doc["nodes"] = [n for n in doc["nodes"] if n["id"] not in {"generate", "output"}]
    doc["edges"] = [e for e in doc["edges"] if e["target"]["node"] not in {"generate", "output"} and e["source"]["node"] != "generate"]
    def add(identifier, kind, *, source_nodes=(), **kwargs):
        row = make_node(identifier, kind, **kwargs)
        row["source_nodes"] = [str(x) for x in source_nodes]
        doc["nodes"].append(row); return row
    def wire(a, ap, b, bp):
        doc["edges"].append(make_edge(a, ap, b, bp))
    def branch(identifier, kind, params, label, x, y, enabled=True, prompt_id="prompt", setup_id="setup", media_id="references", source_nodes=()):
        row = add(identifier, kind, x=x, y=y, title=label, params=params, state="active" if enabled else "disabled", source_nodes=source_nodes)
        wire(setup_id, "setup", identifier, "setup"); wire(prompt_id, "prompt", identifier, "prompt"); wire(media_id, "references", identifier, "references")
        add(identifier + "_output", "output.video", x=x+350, y=y, title=label + " output", state="active" if enabled else "disabled")
        wire(identifier, "result", identifier+"_output", "result")
        return row
    for i, (node_id, decode_id, output_id) in enumerate(((125, 187, 18), (133, 176, 134), (143, 180, 144))):
        branch(f"preview_{i+1}", "hunter.preview", {**common, "preview_count": 1, "seed": common["seed"] + i * 1000},
               f"Preview {i+1} · seed +{i*1000}", 1000, 50+i*190, active(node_id), source_nodes=[node_id, decode_id, output_id])
    selection = next((str(i+1) for i, id_ in enumerate((146, 148, 149)) if active(id_)), "1")
    add("selection", "result.select", x=1390, y=670, params={"selection": {"1":"first", "2":"second", "3":"third"}[selection]}, source_nodes=[146,148,149,147])
    for i, p in enumerate(("first", "second", "third")):
        wire(f"preview_{i+1}", "result", "selection", p)
    upscaler = values(243, ["", "megapixels", 2, 32, True, False, "cuda", "fp16"])
    refine = values(135, [0, "er_sde", 2, .4])
    ratio = math.sqrt(float(upscaler[2]) * 1024 * 1024 / (width * height))
    final_width, final_height = round(width * ratio / 32) * 32, round(height * ratio / 32) * 32
    refine_params = {"candidate": 1, "width": final_width, "height": final_height, "steps": refine[2], "sampler": refine[1],
                     "scheduler": values(360, ["linear_quadratic"], inner=True)[0], "denoise": refine[3],
                     "seed": values(103, [42])[0], "upscaler_chunking": upscaler[4], "upscaler_force_unload": upscaler[5], **sparse(403)}
    add("refine", "hunter.refine", x=1690, y=670, params=refine_params, state=state(135), source_nodes=[135,243,103])
    wire("selection", "result", "refine", "preview")
    # Dedicated setup lets refinement retain its saved upscaler and extra LoRAs.
    setup_copy = copy.deepcopy(next(n for n in doc["nodes"] if n["id"] == "setup")); setup_copy.update(id="refine_setup", title="Refinement model setup", position={"x": 1300, "y": 920})
    doc["nodes"].append(setup_copy)
    for edge in list(doc["edges"]):
        if edge["target"]["node"] == "setup":
            wire(edge["source"]["node"], edge["source"]["port"], "refine_setup", edge["target"]["port"])
    add("latent_upscaler", "component.latent_upscaler", x=1000, y=1030, params=_component("latent_upscaler", upscaler[0], inventory), source_nodes=[243])
    wire("latent_upscaler", "latent_upscaler", "refine_setup", "latent_upscaler"); wire("refine_setup", "setup", "refine", "setup")
    def additional_loras(loader_id, hub, prefix, y):
        rows = values(loader_id, [{}])[0].get("loras", [])
        edge = next(e for e in doc["edges"] if e["target"] == {"node":hub,"port":"model"})
        upstream = edge["source"]["node"]
        doc["edges"].remove(edge)
        for i,row in enumerate(rows):
            identifier = prefix + str(i)
            params = _component("lora",row.get("name"),inventory)
            params.update(strength=row.get("sm",1),enabled=bool(row.get("on",True)) and active(loader_id))
            add(identifier,"model.lora",x=650+i*260,y=y,params=params,source_nodes=[loader_id])
            wire(upstream,"model",identifier,"model"); upstream=identifier
        wire(upstream,"model",hub,"model")
    # The optional TAE per-step preview modifier is not equivalent to a final
    # frame decoder. Keep it in the actual model chain with an explicit adapter
    # blocker when enabled, preserving its saved values while bypassed.
    model_edge = next(e for e in doc["edges"] if e["target"] == {"node":"refine_setup","port":"model"})
    source_model = model_edge["source"]["node"]
    doc["edges"].remove(model_edge)
    add("tae_preview_override","model.unmapped",x=700,y=1100,title="TAE live preview override",params={"class_type":"ModelPreviewOverrideKJ","widgets":values(106)},state=state(106),source_nodes=[106])
    wire(source_model,"model","tae_preview_override","model"); wire("tae_preview_override","model","refine_setup","model")
    additional_loras(553,"refine_setup","refine_lora_",1100)
    add("final_output", "output.video", x=2010, y=670, title="Final video", source_nodes=[34])
    wire("refine", "result", "final_output", "result")
    branch("single", "hunter.single", {**common, "preview_count": 1}, "Single pass", 1000, 1210, active(259), source_nodes=[259,258])
    branch("continuation", "hunter.continuation", {**common, "continuation_overlap": values(297, [22])[0], "preview_count": 1},
           "Continue source video", 1000, 1430, active(296), media_id="continuation_references", source_nodes=[296,297,389,326])
    add("continuation_references", "input.references", x=660, y=1430)
    add("continuation_video", "input.video", x=340, y=1430, params={"name":"Original video", "asset":""}, source_nodes=[417])
    wire("continuation_video", "video", "continuation_references", "source_video")
    dialogue_seconds = values(540, [6])[0]; dialogue_frames = max(5, round(dialogue_seconds*24)); dialogue_frames += (5-dialogue_frames)%17
    dialogue_prompt = "".join([values(526, [""])[0], values(571, [""])[0], "</d> ", values(522, [""])[0], values(527, [""])[0]])
    add("dialogue_prompt", "input.prompt", x=660, y=1650, title="Dialogue prompt", params={"text": dialogue_prompt}, source_nodes=[526,571,522,527,544,545,546,547])
    add("dialogue_references", "input.references", x=660, y=1830)
    voice = values(535, {})
    add("dialogue_voice", "input.audio", x=340, y=1830, title="Voice reference", params={"name": _name(voice.get("audio", "")), "asset":""}, source_nodes=[535])
    wire("dialogue_voice", "audio", "dialogue_references", "reference_audio")
    # Dialogue explicitly uses the base transformer, not the normal branch's adapters.
    add("dialogue_setup", "hub.setup", x=660, y=2040)
    for role in COMPONENTS[:4]: wire(role, role, "dialogue_setup", role)
    additional_loras(552,"dialogue_setup","dialogue_lora_",2230)
    quality = nodes.get(558, {}).get("properties", {}).get("switchState", {}).get("activeIndex", 2)
    size = values({0:559,1:560,2:561}.get(quality,561), [512])[0]
    dialogue_params = {**common, "width":size, "height":size, "frames":dialogue_frames, "reference_size":"match", "steps":values(541,["simple",16,1])[1],
                       "scheduler":values(541,["simple",16,1])[0], "shift_video":values(556,[12,3])[0], "shift_audio":values(556,[12,3])[1],
                       "shift_enabled":True, "sparse_method":"none", "preview_count":3,
                       "audio_start_seconds":voice.get("start_time",0), "audio_end_seconds":voice.get("start_time",0)+voice.get("duration",45)}
    branch("dialogue", "hunter.dialogue", dialogue_params, "Dialogue / voice", 1000, 1830, active(518), prompt_id="dialogue_prompt", setup_id="dialogue_setup", media_id="dialogue_references", source_nodes=[518,540,541,552,556])
    doc["import_issues"] = [
        {"severity":"info", "code":"seed_hunter_graph", "message":"Preserved all three preview branches, preview selection, refinement, single-pass, continuation and dialogue. Disabled and bypassed branches remain editable with their saved settings."},
        {"severity":"warning", "code":"optional_preview_adapter", "message":"The saved TAE live-preview override remains as a bypassed modifier in the refinement model chain. Enabling it requires its runtime adapter; final preview rendering remains available."},
        {"severity":"warning", "code":"output_storage", "message":"Outputs use Odysseus job storage and native video encoding. Original output-folder and codec widgets remain in the source archive."},
        {"severity":"info", "code":"media_rebind", "message":"Media filenames are preserved as labels. Attach or select media on this server before enabling branches that require it."},
    ]
    doc["source"] = {"format":"comfyui", "adapter":"seed_hunter_v251", "document":copy.deepcopy(source)}
    return validate_document(doc)


def _opaque(source, title):
    """Preserve unknown graph connections as explicit blocked operations."""
    from src.comfy_workflow_conversion import Graph
    parsed = Graph(source)
    registry = node_registry()
    doc = {"format":FORMAT,"version":VERSION,"title":title,"nodes":[],"edges":[],
           "source":{"format":"comfyui","document":copy.deepcopy(source)},
           "import_issues":[{"severity":"warning","code":"runtime_adapters","message":"The original nodes, links, widgets and disabled branches are preserved. Operations without a native adapter must be implemented before this graph can run."}]}
    ui = {str(n.get("id")):n for n in source.get("nodes", [])} if isinstance(source.get("nodes"), list) else {}
    for i, node in enumerate(parsed.nodes.values()):
        original = ui.get(node["id"], {})
        pos = original.get("pos", [i%5*300,i//5*200])
        if isinstance(pos,dict): pos=[pos.get("0",0),pos.get("1",0)]
        identifier = "comfy_" + node["id"]
        row = make_node(identifier,"comfy."+node["type"],x=pos[0],y=pos[1],title=original.get("title") or node["type"],params={"inputs":copy.deepcopy(node["inputs"]),"widgets":copy.deepcopy(original.get("widgets_values"))},state={0:"active",2:"disabled",4:"bypassed"}.get(node["mode"],"disabled"))
        descriptor = registry.get(row["type"])
        if descriptor and descriptor.get("execution") == "comfy":
            from src.comfy_workflow_conversion import reference
            parameters = {k:copy.deepcopy(v) for k,v in node["inputs"].items() if not reference(v)}
            widgets = original.get("widgets_values", [])
            leftover = []
            if isinstance(widgets, dict):
                parameters.update(widgets)
            elif isinstance(widgets, list):
                # UI widget order follows INPUT_TYPES scalar/combo controls.
                fields = [f for f in descriptor["params"] if not f["id"].startswith("__odysseus_")]
                cursor = 0
                for f in fields:
                    if cursor >= len(widgets): break
                    parameters[f["id"]] = copy.deepcopy(widgets[cursor]); cursor += 1
                    # Comfy seed controls add a front-end-only randomize widget.
                    if cursor < len(widgets) and widgets[cursor] in ("fixed","increment","decrement","randomize") and (f.get("control_after_generate") or f["id"] in ("seed","noise_seed","seed_start")):
                        parameters["__comfy_control_after_generate_"+f["id"]] = widgets[cursor]; cursor += 1
                leftover = copy.deepcopy(widgets[cursor:])
            row["params"] = parameters
            row["metadata"] = {"package_id":descriptor.get("package_id"),"package_fingerprint":descriptor.get("package_fingerprint"),"unmapped_widgets":leftover}
        row["source_nodes"]=[node["id"]]
        row["ports"]={"inputs":[],"outputs":[]}
        doc["nodes"].append(row)
    byid = {n["id"]:n for n in doc["nodes"]}
    for index, edge in enumerate(parsed.edges):
        left,right="comfy_"+edge["from"],"comfy_"+edge["to"]
        out="output_"+str(edge["output"]); inp=edge["input"]
        doc["edges"].append(make_edge(left,out,right,inp,"comfy_link_"+str(index)))
        for identifier,side,name in ((left,"outputs",out),(right,"inputs",inp)):
            if not any(p["id"]==name for p in byid[identifier]["ports"][side]):
                byid[identifier]["ports"][side].append({"id":name,"name":name,"label":name,"type":"UNKNOWN"})
    # Explicit native terminals keep unknown processing on the reachable path.
    candidates = [n for n in parsed.nodes.values() if n["type"] in {"SaveVideo","VHS_VideoCombine","SaveAudioAdvanced","SaveImage"}]
    for i,n in enumerate(candidates):
        if n["mode"] != 0 or registry.get("comfy."+n["type"],{}).get("output_node"): continue
        output="output_"+n["id"]
        doc["nodes"].append(make_node(output,"output.video",x=1800,y=i*150,title=n["type"]))
        doc["edges"].append(make_edge("comfy_"+n["id"],"output_0",output,"result"))
        row=byid["comfy_"+n["id"]]
        if not any(p["id"]=="output_0" for p in row["ports"]["outputs"]):
            row["ports"]["outputs"].append({"id":"output_0","name":"output_0","type":"UNKNOWN","label":"Result"})
    unknown = [n for n in doc["nodes"] if n["type"].startswith("comfy.") and n["type"] not in registry]
    if not unknown:
        doc["import_issues"] = [{"severity":"info","code":"registered_graph","message":"Imported the actual registered operations, typed connections and settings. Media files must be selected on this server before running."}]
    return validate_document(doc)


def import_comfyui(source, title=None, inventory=None):
    if not isinstance(source, dict):
        raise ValueError("A workflow must be a JSON object")
    title = title or "Imported ComfyUI workflow"
    if source.get("format") == FORMAT:
        return validate_document(source)
    try:
        known = _known_seed_hunter(source,title,inventory)
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        known = None
    if known is not None:
        return known
    from src.comfy_workflow_conversion import Graph, convert_comfyui, OUTPUTS, WIDGETS
    parsed = Graph(source)
    registry = node_registry()
    if any(registry.get("comfy."+n["type"],{}).get("output_node") for n in parsed.nodes.values()):
        # Actual registered Comfy outputs retain their complete operation graph,
        # including custom classes, instead of being flattened into a recipe.
        return _opaque(source,title)
    candidates = [node["id"] for node in parsed.nodes.values() if node["type"] in OUTPUTS and node["mode"] == 0]
    if not candidates:
        return _opaque(source,title)
    converted = []
    for output in candidates:
        report = convert_comfyui(source,inventory,output_node=output)
        if not report.get("convertible") or not report.get("document"):
            return _opaque(source,title)
        native=report["document"]
        counts={}
        for inp in report.get("inputs",[]): counts[inp["field"]]=counts.get(inp["field"],0)+1
        graph=from_native_config(native["config"],native["family"],counts,title=title,components=native.get("components"),inventory=inventory)
        graph["import_issues"]=report.get("issues",[])
        for inp in report.get("inputs",[]):
            slot=inp["field"]
            for node in graph["nodes"]:
                if node["id"].startswith(slot+"__") and not node.get("source_nodes"):
                    node["params"]["name"]=inp["name"]; node["source_nodes"]=[inp.get("node_id", "")]; break
        converted.append((output,graph))
    document={"format":FORMAT,"version":VERSION,"title":title,"nodes":[],"edges":[],"source":{"format":"comfyui","document":copy.deepcopy(source)},"import_issues":[]}
    for output, graph in converted:
        prefix="branch_"+output+"_"
        for node in graph["nodes"]:
            node["id"]=prefix+node["id"]
            node["position"]["y"]+=len(document["nodes"])//20*750
            document["nodes"].append(node)
        for edge in graph["edges"]:
            edge["id"]=prefix+edge["id"]
            edge["source"]["node"]=prefix+edge["source"]["node"]; edge["target"]["node"]=prefix+edge["target"]["node"]
            document["edges"].append(edge)
        document["import_issues"].extend(graph.get("import_issues",[]))
    # Preserve inactive/unsupported branches visibly, not solely in a raw archive.
    for node in parsed.nodes.values():
        if node["mode"] != 0 or node["type"] not in WIDGETS:
            raw=make_node("preserved_"+node["id"],"comfy."+node["type"],x=0,y=1400+len(document["nodes"])*20,
                          params={"inputs":copy.deepcopy(node["inputs"])},state={2:"disabled",4:"bypassed"}.get(node["mode"],"active"))
            raw["source_nodes"]=[node["id"]]; document["nodes"].append(raw)
    return validate_document(document)
