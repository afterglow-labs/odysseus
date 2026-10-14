"""Editable, typed workflow graphs compiled to native jobs and media operations.

The graph is the execution source of truth. Positions and grouping never affect
execution; only parameters, enabled states, and typed edges do. Imported Python
and expressions are never evaluated here.
"""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import PurePosixPath

FORMAT = "odysseus-workflow-graph"
VERSION = 1
MAX_NODES = 800
MAX_EDGES = 4000
MAX_EXACT_INTEGER = 2**53 - 1
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def port(name, kind, *, required=False, multiple=False, label=None):
    return {"id": name, "name": name, "label": label or name.replace("_", " ").title(),
            "type": kind, "required": required, "multiple": multiple}


def field(name, kind="string", default=None, **kwargs):
    return {"id": name, "name": name, "label": kwargs.pop("label", name.replace("_", " ").title()),
            "type": kind, "default": default, **kwargs}


def choice(name, default, choices, **kwargs):
    return field(name, "select", default, choices=list(choices), **kwargs)


def spec(kind, title, category, inputs=(), outputs=(), params=(), **kwargs):
    return {"type": kind, "title": title, "category": category, "inputs": list(inputs),
            "outputs": list(outputs), "params": list(params), **kwargs}


COMPONENTS = ("model", "encoder", "video_vae", "audio_vae", "latent_upscaler")
COMPONENT_TYPES = {"model": "MODEL", "encoder": "ENCODER", "video_vae": "VIDEO_VAE",
                   "audio_vae": "AUDIO_VAE", "latent_upscaler": "UPSCALER"}
GENERATION_FIELDS = [
    choice("mode", "t2va", ("t2va", "fl2va", "ref2va")),
    field("width", "integer", 960, min=64, max=4096, step=32),
    field("height", "integer", 544, min=64, max=4096, step=32),
    field("frames", "integer", 124, min=5, max=362, step=17),
    field("steps", "integer", 20, min=1, max=100),
    field("seed", "integer", 42, min=0, max=9007199254738990),
    choice("sampler", "euler", ("euler", "res_multistep", "er_sde")),
    choice("scheduler", "simple", ("simple", "normal", "beta", "linear_quadratic")),
    field("shift_video", "number", 12, min=.01, max=100),
    field("shift_audio", "number", 3, min=.01, max=100),
    choice("reference_size", "match", ("match", "max")),
    field("gpu", "string", "0", editor="gpu"), field("vae_gpu", "string", "", editor="gpu", label="VAE GPU"),
    field("reference_video_ranges", "array", [], label="Reference clip ranges", description="One {start_seconds, duration_seconds} entry per reference video; an empty list uses whole clips."),
]
HUNTER_FIELDS = [
    field("preview_count", "integer", 3, min=1, max=3),
    field("shift_enabled", "boolean", False),
    field("first_frame_guide", "boolean", False), field("last_frame_guide", "boolean", False),
    field("force_audio", "boolean", False), field("refmod_strengths", "array", [], label="RefMod strengths"),
    choice("attention_backend", "comfy kitchen attention", ("comfy kitchen attention", "pytorch")),
    choice("sparse_method", "none", ("none", "sla", "sol_attn", "vsa")),
    field("sparse_keep_percent", "number", 10, min=1, max=100),
    field("sparse_start", "number", .2, min=0, max=1), field("sparse_end", "number", 1, min=0, max=1),
    field("sparse_min_tokens", "integer", 12288, min=0, max=1000000),
    field("sparse_extra_tokens", "integer", 256, min=0, max=8192),
    choice("sparse_sink_conditioning", "exact_kv_and_rows", ("exact_kv_and_rows", "none")),
    field("ff_chunk_enabled", "boolean", True, label="Enable feed-forward chunking"),
    field("ff_chunk_size", "integer", 0, min=0, max=64),
    field("ff_chunk_threshold", "integer", 2048, min=1, max=1000000),
    field("low_vram_enabled", "boolean", True, label="Enable low-VRAM attention"),
    field("low_vram_chunks", "integer", 0, min=0, max=64),
    choice("continuation_overlap", 22, (5, 22, 39, 56)), choice("output_fps", 24, (24, 48)),
    field("interpolation_enabled", "boolean", False, label="Interpolate output with RIFE"),
    choice("interpolation_fps", 48, (48,), label="Interpolation frame rate"),
    field("source_start_seconds", "number", 0, min=0, max=86400),
    field("source_end_seconds", "number", None, nullable=True, min=0, max=86400),
    field("audio_start_seconds", "number", 0, min=0, max=3600),
    field("audio_end_seconds", "number", None, nullable=True, min=0, max=3600),
]
REFERENCE_PORTS = [port("first_frame", "IMAGE"), port("last_frame", "IMAGE"), port("source_video", "VIDEO"),
                   port("reference_images", "IMAGE", multiple=True), port("reference_videos", "VIDEO", multiple=True),
                   port("reference_audio", "AUDIO", multiple=True), port("reference_mods", "REFMOD", multiple=True)]
GENERATION_INPUTS = [port("setup", "SETUP", required=True), port("prompt", "TEXT", required=True), port("references", "REFERENCES")]
REGISTRY = {s["type"]: s for s in [
    *[spec("component." + role, role.replace("_", " ").title(), "Model setup", outputs=[port(role, COMPONENT_TYPES[role])],
           params=[field("component", default="", editor="component", component_kind=role), field("name", default="", label="Imported filename")]) for role in COMPONENTS],
    spec("model.lora", "Apply LoRA", "Model setup", [port("model", "MODEL", required=True)], [port("model", "MODEL")],
         [field("component", default="", editor="component", component_kind="lora"), field("name", default="", label="Imported filename"),
          field("strength", "number", 1, min=-20, max=20, step=.05), field("enabled", "boolean", True)], bypass={"model": "model"}),
    spec("model.unmapped", "Imported model modifier (adapter needed)", "Imported operations", [port("model", "MODEL", required=True)], [port("model", "MODEL")], [field("class_type", default=""), field("widgets", "json", [])], bypass={"model":"model"}),
    spec("hub.setup", "Model setup hub", "Model setup", [port(role, COMPONENT_TYPES[role], required=role != "latent_upscaler") for role in COMPONENTS], [port("setup", "SETUP")]),
    spec("input.prompt", "Prompt", "Inputs", outputs=[port("prompt", "TEXT")], params=[field("text", "text", "")]),
    *[spec("input." + kind, kind.title(), "Inputs", outputs=[port(kind, kind.upper())],
           params=[field("name", default=""), field("asset", default="", editor="media", media_kind=kind)]) for kind in ("image", "video", "audio", "refmod")],
    spec("input.references", "Reference bundle", "Inputs", REFERENCE_PORTS, [port("references", "REFERENCES")]),
    spec("media.trim", "Trim video", "Video processing", [port("video", "VIDEO", required=True)], [port("video", "VIDEO")],
         [field("start", "number", 0, min=0, max=86400), field("end", "number", None, nullable=True, min=0, max=86400)], bypass={"video": "video"}),
    spec("media.resize", "Resize video", "Video processing", [port("video", "VIDEO", required=True)], [port("video", "VIDEO")],
         [field("width", "integer", 960, min=16, max=4096, step=2), field("height", "integer", 544, min=16, max=4096, step=2),
          choice("fit", "contain", ("contain", "cover", "stretch"))], bypass={"video": "video"}),
    spec("h3.generate", "H3 Generate", "Generation", GENERATION_INPUTS, [port("result", "RESULT")], GENERATION_FIELDS),
    *[spec("hunter." + stage, "Seed Hunter " + stage.title(), "Generation", GENERATION_INPUTS, [port("result", "RESULT")],
           [{**p, "default": {"mode": "ref2va", "width": 480, "height": 864, "frames": 73, "steps": 6,
                              "sampler": "er_sde", "scheduler": "beta"}.get(p["id"], p["default"])} for p in GENERATION_FIELDS] + HUNTER_FIELDS)
      for stage in ("preview", "single", "continuation", "dialogue")],
    spec("hunter.refine", "Refine selected preview", "Generation", [port("preview", "RESULT", required=True), port("setup", "SETUP")], [port("result", "RESULT")],
         [field("candidate", "integer", 1, min=1, max=3), field("width", "integer", 1088, min=256, max=4096, step=32),
          field("height", "integer", 1952, min=256, max=4096, step=32), field("steps", "integer", 2, min=1, max=100),
          field("seed", "integer", 42, min=0, max=9007199254738990), choice("sampler", "er_sde", ("euler", "res_multistep", "er_sde")),
          choice("scheduler", "linear_quadratic", ("simple", "normal", "beta", "linear_quadratic")),
          field("denoise", "number", .4, min=0, max=1), field("upscaler_chunking", "boolean", True),
          field("upscaler_force_unload", "boolean", False)], bypass={"result": "preview"}),
    spec("bfs.components", "BFS component hub", "Model setup", outputs=[port("setup", "SETUP")], params=[field(role, default="", editor="component", component_kind=role, inventory_source="bfs") for role in ("model", "model_low", "encoder", "video_vae", "audio_vae", "connector", "lora", "lora_low", "speed_lora")]),
    spec("bfs.generate", "BFS face/head swap", "Generation", [port("setup", "SETUP", required=True), port("prompt", "TEXT"),
         port("source_video", "VIDEO", required=True), port("identity_image", "IMAGE", required=True), port("last_frame", "IMAGE"), port("mask_video", "VIDEO")],
         [port("result", "RESULT")], [choice("workflow_id", "h3_head_swap", ("h3_head_swap", "ltx2_v1", "ltx2_v2", "ltx23_v3", "ltx25_v1", "ltx25_v11", "wan22_head_swap")), field("gpu", default="0", editor="gpu"),
          field("width", "integer", 640, min=256, max=1920, step=32), field("height", "integer", 384, min=256, max=1920, step=32),
          field("frames", "integer", 124, min=5, max=362), field("steps", "integer", 20, min=1, max=100),
          field("seed", "integer", 42, min=0, max=4294967295), field("start_seconds", "number", 0, min=0, max=86400),
          choice("audio_mode", "source", ("source","silent")), choice("resize_mode", "contain", ("contain","crop")),
          field("lora_scale", "number", 1, min=.01, max=4), field("speed_lora_scale", "number", 1, min=0, max=4),
          field("cfg", "number", 5, min=1, max=20), choice("sampler", "euler", ("euler","res_multistep")),
          choice("scheduler", "beta", ("beta","simple","normal")), field("shift_video", "number", 12, min=.01,max=100),
          field("shift_audio", "number", 3, min=.01,max=100), choice("reference_size", "match", ("match","max"))]),
    spec("result.select", "Choose result", "Routing", [port("first", "RESULT"), port("second", "RESULT"), port("third", "RESULT")], [port("result", "RESULT")], [choice("selection", "first", ("first", "second", "third"))]),
    spec("h3.seed_scout", "H3 Seed Scout", "Generation", GENERATION_INPUTS, [port("result", "RESULT")], GENERATION_FIELDS + [p for p in HUNTER_FIELDS if p["id"] in {"first_frame_guide", "last_frame_guide", "force_audio", "refmod_strengths"}] + [field("scout_count", "integer", 4, min=1, max=32), field("scout_step", "integer", 1, min=1, max=100), choice("scout_mode", "interactive", ("interactive", "scout", "final")), field("scout_timeout", "integer", 0, min=0, max=86400), choice("scout_preview_mode", "vae", ("vae", "latent2rgb"))]),
    spec("output.video", "Video output", "Outputs", [port("result", "RESULT|VIDEO", required=True)], params=[field("name", default="Video")]),
]}



def _port_type(value):
    aliases = {"STRING":"TEXT", "INT":"INTEGER", "FLOAT":"NUMBER", "BOOLEAN":"BOOLEAN"}
    return aliases.get(str(value), str(value))


def node_registry(manifests=None):
    """Translate discovered class metadata into editor and compiler manifests."""
    result = copy.deepcopy(REGISTRY)
    if manifests is None:
        try:
            from src.comfy_node_manifest import load_manifests
            manifests = load_manifests()
        except ImportError:
            manifests = []
    declarations = {}
    for package in manifests:
        if package.get("status") not in {"ready", "partial"}:
            continue
        info = package.get("package") or package.get("source") or {}
        for node in package.get("nodes", []):
            name = node.get("class_type")
            if isinstance(name, str) and name:
                declarations.setdefault(name, []).append((info, node))
    for name, candidates in declarations.items():
        # A downloaded /object_info schema is useful before installation; it
        # must not shadow, or collide with, its locally discovered package.
        bound = [row for row in candidates if row[0].get("id") and row[0].get("fingerprint")
                 and (row[0].get("source_path") or row[0].get("package_path"))]
        if bound:
            unique = {}
            for package, node in bound:
                unique[(package["id"], package["fingerprint"])] = (package, node)
            candidates = list(unique.values())
        package, node = candidates[0]
        ins, outs, params = [], [], []
        for item in node.get("inputs", []):
            if item.get("section") == "hidden":
                continue
            key = item.get("name")
            if not isinstance(key, str):
                continue
            raw_type = item.get("type", "UNKNOWN")
            kind = _port_type(raw_type)
            choices = item.get("choices")
            if raw_type == "COMBO" and choices:
                if all(isinstance(value,str) for value in choices): kind = "TEXT"
                elif all(type(value) is int for value in choices): kind = "INTEGER"
                elif all(type(value) in (int,float) for value in choices): kind = "NUMBER"
            widget = item.get("is_widget", False)
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            input_port = port(key, kind, required=item.get("section") == "required" and not widget,
                              label=metadata.get("display_name") or key)
            input_port.update(is_list=bool(item.get("is_list")), force_input=bool(item.get("force_input")),
                              default_input=bool(item.get("default_input")), metadata=copy.deepcopy(metadata),
                              raw=copy.deepcopy(item.get("raw")))
            ins.append(input_port)
            if widget:
                choices = item.get("choices")
                default = item.get("default", metadata.get("default"))
                if default is None and choices:
                    default = choices[0]
                ftype = {"INTEGER":"integer", "NUMBER":"number", "BOOLEAN":"boolean", "TEXT":"text"}.get(kind,"string")
                p = field(key, "select" if choices else ftype, default)
                if choices: p["choices"] = copy.deepcopy(choices)
                p.update(metadata=copy.deepcopy(metadata), raw=copy.deepcopy(item.get("raw")),
                         force_input=bool(item.get("force_input")), default_input=bool(item.get("default_input")))
                for attr in ("min", "max", "step", "multiline", "tooltip", "display", "round", "precision", "control_after_generate", "dynamicPrompts"):
                    if attr in metadata: p[attr] = metadata[attr]
                if metadata.get("tooltip"): p["description"] = metadata["tooltip"]
                media_kind = next((kind for kind in ("image","video","audio") if metadata.get(kind+"_upload")), None)
                if media_kind:
                    p.update(editor="media",media_kind=media_kind)
                elif widget and not (raw_type == "COMBO" and key.lower() == "model_path") and re.search(r"load|read|input",name,re.I) and (key.lower() in {"file","filename","filepath","path","directory","folder","image","video","audio"} or key.lower().endswith(("_path","_file","_directory","_folder"))):
                    p["requires_media_adapter"] = True
                    p["description"] = "This loader needs an Odysseus file binding adapter before it can access server files."
                params.append(p)
        for index, item in enumerate(node.get("outputs", [])):
            output_port = port("output_"+str(index), _port_type(item.get("type", "UNKNOWN")),label=item.get("name") or "Output "+str(index+1))
            output_port.update(is_list=bool(item.get("is_list")), metadata=copy.deepcopy(item))
            outs.append(output_port)
        if node.get("output_node"):
            outs.append(port("result","RESULT",label="Saved result"))
            params.append(field("__odysseus_device", default="cpu", label="Execution device", editor="gpu", description="CPU or the selected physical GPU for this graph."))
        bypass = {}
        for output in outs:
            matches = [i for i in ins if i["type"] == output["type"]]
            if len(matches) == 1: bypass[output["id"]] = matches[0]["id"]
        result["comfy."+name] = spec("comfy."+name,node.get("display_name") or name,node.get("category") or "ComfyUI packages",ins,outs,params,
            package_id=package.get("id"),package_fingerprint=package.get("fingerprint"),class_type=name,
            execution="comfy",execution_status="collision" if bound and len(candidates)>1 else "available" if package.get("id") and package.get("fingerprint") and (package.get("source_path") or package.get("package_path")) else "unbound",output_node=bool(node.get("output_node")),
            function=node.get("function"),bypass=bypass, input_is_list=bool(node.get("input_is_list")),
            raw_schema=copy.deepcopy(node.get("raw_schema",{})), hooks=copy.deepcopy(node.get("hooks",{})),
            implementation=copy.deepcopy(node.get("implementation",{})),
            description=node.get("description", ""),
            discovery_issue="Multiple installed packages register this class" if bound and len(candidates)>1 else None)
    return result


def catalog():
    return {"format": FORMAT, "version": VERSION, "node_types": list(node_registry().values()),
            "templates": [{"id": key, "title": title} for key, title in (("blank", "Empty workflow"), ("h3", "H3 video"),
                ("seed_hunter", "Seed Hunter previews and refinement"), ("seed_scout", "Seed Scout interactive search"), ("media", "Trim and resize video"))]}


def make_node(identifier, kind, *, params=None, x=0, y=0, title=None, state="active"):
    row = {"id": identifier, "type": kind, "position": {"x": x, "y": y}, "params": copy.deepcopy(params or {}), "state": state}
    if title:
        row["title"] = title
    return row


def make_edge(source, source_port, target, target_port, identifier=None):
    return {"id": identifier or f"{source}.{source_port}:{target}.{target_port}",
            "source": {"node": source, "port": source_port}, "target": {"node": target, "port": target_port}}


def new_document(title="Untitled workflow", template="h3"):
    doc = {"format": FORMAT, "version": VERSION, "title": title, "nodes": [], "edges": []}
    if template == "blank":
        return doc
    def add(identifier, kind, **kw):
        doc["nodes"].append(make_node(identifier, kind, **kw))
    def wire(a, ap, b, bp):
        doc["edges"].append(make_edge(a, ap, b, bp))
    if template == "media":
        add("video", "input.video", x=40, y=180)
        add("trim", "media.trim", x=300, y=180)
        add("resize", "media.resize", x=560, y=180)
        add("output", "output.video", x=820, y=180)
        wire("video", "video", "trim", "video"); wire("trim", "video", "resize", "video"); wire("resize", "video", "output", "result")
        return doc
    if template not in {"h3", "seed_hunter", "seed_scout"}:
        raise ValueError("Unknown workflow template")
    for index, role in enumerate(COMPONENTS[:4]):
        add(role, "component." + role, x=40, y=40 + index * 140)
        wire(role, role, "setup", role)
    add("setup", "hub.setup", x=340, y=190)
    add("prompt", "input.prompt", x=340, y=390)
    add("references", "input.references", x=340, y=540)
    generation = "hunter.preview" if template == "seed_hunter" else "h3.seed_scout" if template == "seed_scout" else "h3.generate"
    add("generate", generation, x=660, y=280, params={"mode":"ref2va"} if template == "seed_scout" else None)
    for source, p in (("setup", "setup"), ("prompt", "prompt"), ("references", "references")):
        wire(source, p, "generate", p)
    add("output", "output.video", x=1260, y=280)
    if template == "seed_hunter":
        add("refine", "hunter.refine", x=960, y=280, state="bypassed")
        wire("generate", "result", "refine", "preview"); wire("setup", "setup", "refine", "setup"); wire("refine", "result", "output", "result")
    else:
        wire("generate", "result", "output", "result")
    return doc


def validate_document(document):
    if not isinstance(document, dict) or document.get("format") != FORMAT or document.get("version") != VERSION:
        raise ValueError("Use an Odysseus workflow graph")
    if not isinstance(document.get("title", ""), str) or len(document.get("title", "")) > 300:
        raise ValueError("Workflow title must be at most 300 characters")
    if not isinstance(document.get("nodes"), list) or len(document["nodes"]) > MAX_NODES:
        raise ValueError("Workflow exceeds the node limit")
    if not isinstance(document.get("edges"), list) or len(document["edges"]) > MAX_EDGES:
        raise ValueError("Workflow exceeds the connection limit")
    # Ensure JSON-safe snapshots, rejecting non-finite values and Python objects.
    try:
        encoded = json.dumps(document, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Workflow must contain finite JSON values") from exc
    if len(encoded) > 12 * 1024 * 1024:
        raise ValueError("Workflow exceeds 12 MB")
    ids = set()
    for row in document["nodes"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not IDENTIFIER.fullmatch(row["id"])
                or row["id"] in ids or not isinstance(row.get("type"), str) or not 0 < len(row["type"]) <= 240
                or not isinstance(row.get("params", {}), dict)):
            raise ValueError("Invalid or duplicate workflow node")
        ids.add(row["id"])
        if row.get("state", "active") not in {"active", "disabled", "bypassed"}:
            raise ValueError("Unknown node state")
        position = row.get("position", {})
        if not isinstance(position, dict) or any(type(position.get(k, 0)) not in (float, int) for k in ("x", "y")):
            raise ValueError("Invalid node position")
    edge_ids = set()
    for row in document["edges"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in edge_ids or len(row["id"]) > 500:
            raise ValueError("Invalid or duplicate connection")
        edge_ids.add(row["id"])
        for end in ("source", "target"):
            point = row.get(end)
            if (not isinstance(point, dict) or point.get("node") not in ids or not isinstance(point.get("port"), str)
                    or not 0 < len(point["port"]) <= 200):
                raise ValueError("Connection references a missing node or port")
    return copy.deepcopy(document)


def _basename(value):
    return PurePosixPath(str(value).replace("\\", "/")).name


def _params(node, registry=None):
    descriptor = (registry or REGISTRY).get(node["type"], {})
    return {**{p["id"]: copy.deepcopy(p["default"]) for p in descriptor.get("params", [])}, **node.get("params", {})}


class _CompileFailure(Exception):
    def __init__(self, code, message, node):
        self.code, self.message, self.node = code, message, node


def compile_graph(document, output_ids=None, inventory=None):
    """Resolve the actual connected graph into an ordered, immutable job plan.

    ``inputs`` values reference input-node IDs or preceding media-step IDs.
    Native component IDs are validated against inventory when supplied. Imported
    filenames resolve only if exactly one matching role is available.
    """
    registry = node_registry()
    document = validate_document(document)
    nodes = {node["id"]: node for node in document["nodes"]}
    issues, plan, outputs, incoming, cache, visiting, reached = [], [], [], {}, {}, set(), set()
    comfy_result = None
    def report(code, message, node=None, severity="error"):
        row = {"severity": severity, "code": code, "message": message,
               "node_id": node["id"] if node else None, "node_type": node["type"] if node else None}
        if row not in issues:
            issues.append(row)
    def fail(code, message, node):
        raise _CompileFailure(code, message, node)
    def check_integer_precision(node, value, label):
        if (type(value) is int or type(value) is float and value.is_integer()) and abs(value) > MAX_EXACT_INTEGER:
            fail("parameter_precision", f"{label} exceeds the exact integer precision of the web and iOS editors. "
                 f"Choose an integer between {-MAX_EXACT_INTEGER} and {MAX_EXACT_INTEGER}; the imported value has not been clamped.", node)
    for edge in document["edges"]:
        incoming.setdefault((edge["target"]["node"], edge["target"]["port"]), []).append(edge["source"])
        left, right = nodes[edge["source"]["node"]], nodes[edge["target"]["node"]]
        if left["type"] in registry and right["type"] in registry:
            out = next((p for p in registry[left["type"]]["outputs"] if p["id"] == edge["source"]["port"]), None)
            inp = next((p for p in registry[right["type"]]["inputs"] if p["id"] == edge["target"]["port"]), None)
            if out is None or inp is None:
                report("unknown_port", "This connection uses a missing port", right)
            elif "*" not in {out["type"],inp["type"]} and not set(out["type"].split("|")) & set(inp["type"].split("|")):
                report("port_type", f"Cannot connect {out['type']} to {inp['type']}", right)
    def connected(node, port_name, required=False, multiple=False):
        sources = incoming.get((node["id"], port_name), [])
        if not sources:
            if required:
                fail("missing_input", f"Connect {port_name.replace('_', ' ')}", node)
            return [] if multiple else None
        if len(sources) > 1 and not multiple:
            fail("multiple_inputs", f"{port_name.replace('_', ' ')} accepts one connection", node)
        values = [evaluate(source["node"], source["port"]) for source in sources]
        return values if multiple else values[0]
    def component(node, role):
        params = _params(node, registry)
        selected, name = params.get("component", ""), params.get("name", "")
        if not isinstance(selected, str) or not isinstance(name, str):
            fail("component", "Choose an installed component", node)
        inv = inventory
        if isinstance(inv, dict) and "components" not in inv:
            inv = inv.get("h3", {})
        available = inv.get("components", []) if isinstance(inv, dict) else None
        if available is not None:
            if selected:
                matches = [r for r in available if r.get("role") == role and (r.get("id") == selected or selected in r.get("aliases", []))]
            else:
                matches = [r for r in available if r.get("role") == role and _basename(r.get("name", "")) == _basename(name)] if name else []
            if len(matches) != 1:
                fail("ambiguous_component" if len(matches) > 1 else "missing_component", f"Choose an installed {role.replace('_', ' ')}" + (f" for {_basename(name)}" if name else ""), node)
            return matches[0]["id"]
        if selected:
            return selected
        fail("missing_component", f"Choose an installed {role.replace('_', ' ')}" + (f" for {_basename(name)}" if name else ""), node)
    def check_params(node, params):
        for f in registry[node["type"]]["params"]:
            if f.get("editor") == "media": continue
            if registry[node["type"]].get("execution") == "comfy" and incoming.get((node["id"],f["id"])):
                continue  # A real connection overrides the inactive saved widget.
            value = params.get(f["id"])
            if value is None and f.get("nullable"):
                continue
            kind = f["type"]
            if kind in {"integer", "select"} or kind == "number" and type(value) is int:
                check_integer_precision(node, value, f["label"])
            if kind in {"number", "integer"}:
                if type(value) not in (float, int) or not math.isfinite(value) or kind == "integer" and type(value) is not int:
                    fail("parameter", f"{f['label']} must be a finite {kind}", node)
                if value < f.get("min", -math.inf) or value > f.get("max", math.inf):
                    fail("parameter", f"{f['label']} is outside its supported range", node)
            elif kind == "boolean" and type(value) is not bool:
                fail("parameter", f"{f['label']} must be on or off", node)
            elif kind == "select" and value not in f["choices"]:
                fail("parameter", f"Choose a supported {f['label'].lower()}", node)
            elif kind in {"string", "text"} and not isinstance(value, str):
                fail("parameter", f"{f['label']} must be text", node)
    def dependencies(media):
        planned = {row["id"] for row in plan}
        return list(dict.fromkeys(ref for values in media.values() for ref in values if ref in planned))
    def compile_comfy():
        nonlocal comfy_result
        if comfy_result is not None:
            return comfy_result
        api, external, media, packages, pending, roots = {}, {}, {}, {}, set(), []
        # All selected terminal Comfy nodes share one PromptExecutor run.
        def find_roots(identifier, seen=None):
            seen = seen or set()
            if identifier in seen: return
            seen.add(identifier)
            node=nodes[identifier]; descriptor=registry.get(node["type"],{})
            if node.get("state","active") == "disabled": return
            if descriptor.get("execution") == "comfy" and descriptor.get("output_node") and node.get("state","active") == "active":
                if identifier not in roots: roots.append(identifier)
            for (target,_),sources in incoming.items():
                if target == identifier:
                    for source in sources: find_roots(source["node"],seen)
        for terminal in chosen: find_roots(terminal["id"])
        def add_node(identifier):
            if identifier in api: return
            node=nodes[identifier]; descriptor=registry.get(node["type"],{})
            reached.add(identifier)
            if identifier in pending: fail("cycle","Connections form a cycle",node)
            if descriptor.get("execution") != "comfy": fail("comfy_boundary","This operation cannot be passed as a live Comfy object",node)
            if descriptor.get("execution_status") == "collision": fail("package_collision",descriptor["discovery_issue"],node)
            if descriptor.get("execution_status") == "unbound": fail("package_unbound","Bind this node metadata to an installed package before running it",node)
            if descriptor.get("implementation",{}).get("function_present") is False:
                fail("python_function_missing","This node has no executable Python function; frontend-only behavior needs an adapter",node)
            if any(p.get("requires_media_adapter") for p in descriptor["params"]):
                fail("media_adapter_missing","This filesystem loader needs an Odysseus media adapter; imported raw file paths are not executed",node)
            if node.get("metadata",{}).get("unmapped_widgets"): fail("widget_schema","Some saved widgets could not be mapped to this installed node schema",node)
            supported_params = {p["id"] for p in descriptor["params"]} | {p["id"] for p in descriptor["inputs"]}
            unknown_params = [key for key in node.get("params",{}) if key not in supported_params and not key.startswith("__comfy_control_after_generate_")]
            if unknown_params:
                fail("input_schema", "Saved inputs are missing from the installed schema: "+", ".join(sorted(unknown_params)),node)
            if node.get("state","active") != "active": fail("disabled_input","Enable this operation or use an unambiguous pass-through",node)
            pending.add(identifier)
            params=_params(node,registry)
            check_params(node,params)
            if descriptor.get("class_type") == "MiniMaxH3SeedScoutSampler" and (params.get("mode", "interactive") == "interactive" or incoming.get((identifier,"mode"))):
                fail("interactive_adapter", "Use the native Seed Scout node for interactive selection; the installed Comfy node has no Odysseus selection bridge", node)
            values={}
            for p in descriptor["inputs"]:
                sources=incoming.get((identifier,p["id"]),[])
                field_descriptor = next((f for f in descriptor["params"] if f["id"] == p["id"]), {})
                if field_descriptor.get("editor") == "media":
                    if sources: fail("media_binding", "Attach a file to this loader's media picker instead of connecting a filename",node)
                    binding="asset_"+str(len(media)); media[binding]=[identifier+"::"+p["id"]]
                    external.setdefault(identifier,{})[p["id"]]={"kind":"FILE","binding":binding,"media_kind":field_descriptor["media_kind"]}
                    continue
                if len(sources)>1: fail("multiple_inputs","A Comfy input accepts one typed connection",node)
                if not sources:
                    if p["id"] in params and params[p["id"]] is not None:
                        if p["type"] == "INTEGER":
                            check_integer_precision(node, params[p["id"]], p["label"])
                        values[p["id"]]=copy.deepcopy(params[p["id"]])
                    elif p["required"]: fail("missing_input","Connect "+p["label"],node)
                    continue
                source=resolve_bypass(sources[0])
                upstream=nodes[source["node"]]; upstream_descriptor=registry.get(upstream["type"],{})
                if upstream_descriptor.get("execution") == "comfy":
                    if not source["port"].startswith("output_"): fail("comfy_boundary","A saved result cannot replace a live Comfy value",node)
                    try: slot=int(source["port"].removeprefix("output_"))
                    except ValueError: fail("unknown_port","Invalid Comfy output slot",upstream)
                    add_node(upstream["id"])
                    values[p["id"]]=[upstream["id"],slot]
                elif p["type"] in {"IMAGE","VIDEO","AUDIO"}:
                    refs=evaluate(source["node"],source["port"])
                    if not isinstance(refs,list): fail("comfy_boundary","Connect an attached or processed media input",node)
                    binding="asset_"+str(len(media)); media[binding]=refs
                    external.setdefault(identifier,{})[p["id"]]={"kind":p["type"],"binding":binding}
                elif p["type"] in {"TEXT","INTEGER","NUMBER","BOOLEAN"}:
                    values[p["id"]]=evaluate(source["node"],source["port"])
                else:
                    fail("comfy_boundary","Model, encoder, latent and conditioning connections must stay inside the same Comfy graph",node)
            api[identifier]={"class_type":descriptor["class_type"],"inputs":values}
            packages[descriptor.get("package_id")]=descriptor.get("package_fingerprint")
            pending.remove(identifier)
        def resolve_bypass(source, seen=None):
            seen=seen or set(); node=nodes[source["node"]]
            if node["id"] in seen: fail("cycle","Bypassed connections form a cycle",node)
            seen.add(node["id"])
            if node.get("state","active") != "bypassed": return source
            descriptor=registry.get(node["type"],{})
            inp=descriptor.get("bypass",{}).get(source["port"])
            paths=incoming.get((node["id"],inp),[]) if inp else []
            if len(paths)!=1: fail("cannot_bypass","This operation has no unambiguous typed pass-through",node)
            reached.add(node["id"])
            return resolve_bypass(paths[0],seen)
        if not roots: fail("comfy_output","Connect an enabled Comfy output operation to save the result",nodes[next(iter(nodes))])
        for identifier in roots: add_node(identifier)
        devices = {_params(nodes[identifier],registry).get("__odysseus_device", document.get("execution",{}).get("device","cpu")) for identifier in roots}
        if len(devices) != 1: fail("execution_device", "All outputs in one Comfy graph must use the same execution device", nodes[roots[0]])
        device = devices.pop()
        if not isinstance(device,str) or not device: fail("execution_device","Choose CPU or a GPU for this graph",nodes[roots[0]])
        identity="comfy_execution"
        while identity in nodes: identity+="_run"
        plan.append({"id":identity,"node_type":"comfy.graph","family":"comfy","operation":"execute",
                     "config":{"prompt":api,"output_nodes":roots,"gpu":device,"packages":[{"id":key,"fingerprint":value} for key,value in packages.items()],"external_inputs":external},
                     "inputs":media,"dependencies":dependencies(media)})
        comfy_result={"step":identity}
        return comfy_result

    def evaluate(identifier, output):
        key = (identifier, output)
        if key in cache:
            return copy.deepcopy(cache[key])
        node = nodes[identifier]; kind = node["type"]; reached.add(identifier)
        if key in visiting:
            fail("cycle", "Connections form a cycle; disconnect the feedback loop", node)
        if kind not in registry:
            fail("unsupported_node", "This imported operation needs a runtime adapter: " + kind, node)
        descriptor = registry[kind]
        if output not in {p["id"] for p in descriptor["outputs"]}:
            fail("unknown_port", "This output port does not exist", node)
        if node.get("state") == "disabled":
            fail("disabled_input", "An enabled branch depends on this disabled operation", node)
        visiting.add(key)
        try:
            if node.get("state") == "bypassed":
                bypass = descriptor.get("bypass", {}).get(output)
                if not bypass:
                    fail("cannot_bypass", "This operation has no compatible pass-through connection", node)
                value = connected(node, bypass, required=True)
            else:
                params = _params(node, registry); check_params(node, params)
                if descriptor.get("execution") == "comfy":
                    value = compile_comfy()
                elif kind.startswith("component."):
                    role = kind.split(".")[1]; value = {"component": component(node, role)}
                    if role == "model": value["loras"] = []
                elif kind == "model.lora":
                    value = connected(node, "model", required=True)
                    if params["enabled"]:
                        value.setdefault("loras", []).append({"id": component(node, "lora"), "strength": params["strength"]})
                elif kind == "model.unmapped":
                    fail("unsupported_node", "Runtime adapter needed for " + str(params.get("class_type", "this imported modifier")), node)
                elif kind == "hub.setup":
                    value = {}
                    for role in COMPONENTS:
                        item = connected(node, role, required=role != "latent_upscaler")
                        if item is not None:
                            value[role] = item["component"]
                            if role == "model": value["loras"] = item.get("loras", [])
                elif kind == "bfs.components":
                    value = {"bfs_components": {k:v for k,v in params.items() if v}}
                elif kind == "input.prompt":
                    value = params["text"]
                elif kind.startswith("input.") and kind != "input.references":
                    value = [identifier]
                elif kind == "input.references":
                    value = {}
                    for p in REFERENCE_PORTS:
                        item = connected(node, p["id"], multiple=p["multiple"])
                        if item:
                            value[p["id"]] = [v for group in item for v in group] if p["multiple"] else item
                elif kind.startswith("media."):
                    media = {"video": connected(node, "video", required=True)}
                    if kind == "media.trim" and params["end"] is not None and params["end"] <= params["start"]:
                        fail("trim_range", "Trim end must be after its start", node)
                    if kind == "media.resize" and (params["width"] % 2 or params["height"] % 2):
                        fail("resize", "Video dimensions must be even numbers", node)
                    plan.append({"id": identifier, "node_type": kind, "family": "media", "operation": kind.split(".")[1],
                                 "params": params, "inputs": media, "dependencies": dependencies(media)})
                    value = [identifier]
                elif kind == "result.select":
                    value = connected(node, params["selection"], required=True)
                elif kind == "hunter.refine":
                    source = connected(node, "preview", required=True)
                    prior = next((row for row in plan if row["id"] == source.get("step")), None)
                    if prior is None or prior["node_type"] != "hunter.preview":
                        fail("refine_source", "Refinement requires a Seed Hunter preview result", node)
                    if params["candidate"] > prior["config"]["seed_hunter"].get("preview_count", 3):
                        fail("candidate", "The selected candidate is not generated by this preview branch", node)
                    setup = connected(node, "setup")
                    cfg = {k: params[k] for k in ("width", "height", "steps", "seed", "sampler", "scheduler")}
                    cfg["seed_hunter"] = {k: params[k] for k in ("denoise", "upscaler_chunking", "upscaler_force_unload")}
                    # Preserve mapped optional refinement controls without copying arbitrary config.
                    from src.h3_seed_hunter import FINAL_OPTIONS
                    cfg["seed_hunter"].update({k: params[k] for k in FINAL_OPTIONS if k in params})
                    if setup:
                        # The native refine API reuses the preview's base assets.
                        for role in COMPONENTS[:4]:
                            if setup.get(role) != prior["config"].get(role):
                                fail("refine_setup", "Refinement must use the preview's model, encoder and VAEs", node)
                        cfg["loras"] = setup.get("loras", [])
                        if setup.get("latent_upscaler"): cfg["latent_upscaler"] = setup["latent_upscaler"]
                    plan.append({"id": identifier, "node_type": kind, "family": "h3", "operation": "refine", "config": cfg,
                                 "inputs": {}, "depends_on": source["step"], "dependencies": [source["step"]], "candidate": params["candidate"]})
                    value = {"step": identifier}
                elif kind == "bfs.generate":
                    setup = connected(node, "setup", required=True)
                    from src.bfs_video import BY_ID
                    wf = BY_ID.get(params["workflow_id"])
                    if not wf:
                        fail("bfs_workflow", "Choose a supported BFS workflow family", node)
                    cfg = {k: params[k] for k in ({"workflow_id", "gpu"} | {c["key"] for c in wf["controls"]}) if k in params}
                    cfg["prompt"] = connected(node, "prompt") or ""
                    cfg["components"] = dict(setup.get("bfs_components", {role: setup[role] for role in COMPONENTS[:4] if role in setup}))
                    loras = setup.get("loras", [])
                    if loras:
                        cfg["components"]["lora"] = loras[0]["id"]; cfg["lora_scale"] = loras[0]["strength"]
                    if len(loras) > 1:
                        cfg["components"]["speed_lora"] = loras[1]["id"]; cfg["speed_lora_scale"] = loras[1]["strength"]
                    if len(loras) > 2:
                        fail("bfs_loras", "This BFS runtime supports a head-swap adapter and one speed adapter", node)
                    declared_slots = {slot["key"] for slot in wf["slots"]}
                    if cfg["components"].keys() - declared_slots:
                        fail("bfs_components", "This setup includes components not used by the selected BFS family", node)
                    bfs_inventory = inventory.get("bfs", {}) if isinstance(inventory, dict) and "bfs" in inventory else None
                    for slot in wf["slots"]:
                        selected = cfg["components"].get(slot["key"], "")
                        if not selected and slot["required"]:
                            fail("missing_component", "Choose " + slot["label"], node)
                        if selected and (not isinstance(selected, str) or bfs_inventory is not None and not any(c.get("id") == selected or selected in c.get("aliases", []) for c in bfs_inventory.get("components", []))):
                            fail("missing_component", "Choose an installed " + slot["label"], node)
                    media = {p["id"]: connected(node, p["id"], required=p["required"]) for p in descriptor["inputs"] if p["type"] in {"IMAGE", "VIDEO"}}
                    media = {k: v for k, v in media.items() if v}
                    plan.append({"id": identifier, "node_type": kind, "family": "bfs", "operation": "submit", "config": cfg, "inputs": media, "dependencies": dependencies(media)})
                    value = {"step": identifier}
                else:
                    setup = connected(node, "setup", required=True)
                    prompt = connected(node, "prompt", required=True)
                    if not prompt.strip():
                        fail("empty_prompt", "Enter a prompt before running", node)
                    media = connected(node, "references") or {}
                    cfg = {p["id"]: params[p["id"]] for p in GENERATION_FIELDS}
                    cfg.update(setup, prompt=prompt)
                    if kind == "h3.seed_scout":
                        cfg["recipe"] = "seed_hunter"
                        cfg["seed_hunter"] = {"stage": "scout", "shift_enabled": True, **{k: params[k] for k in ("scout_count", "scout_step", "scout_mode", "scout_timeout", "scout_preview_mode")}}
                        cfg["seed_hunter"].update({k: params[k] for k in ("first_frame_guide", "last_frame_guide", "force_audio", "refmod_strengths") if params[k]})
                    elif kind.startswith("hunter."):
                        stage = kind.split(".")[1]
                        cfg["mode"] = "ref2va"; cfg["recipe"] = "seed_hunter"
                        cfg["seed_hunter"] = {p["id"]: params[p["id"]] for p in HUNTER_FIELDS}
                        cfg["seed_hunter"]["stage"] = stage
                        options = cfg["seed_hunter"]
                        if not options.pop("ff_chunk_enabled", True): options["ff_chunk_size"] = 0
                        if not options.pop("low_vram_enabled", True): options["low_vram_chunks"] = 0
                        interpolated = options.pop("interpolation_enabled", False)
                        fps = options.pop("interpolation_fps", 48)
                        if interpolated: options["output_fps"] = fps
                        from scripts.h3_seed_hunter_controls import validate_options
                        cfg["seed_hunter"] = validate_options(options)
                    from scripts.h3_reference_video import validate_ranges
                    cfg["reference_video_ranges"] = validate_ranges(cfg["reference_video_ranges"], len(media.get("reference_videos", [])))
                    if cfg["width"] % 32 or cfg["height"] % 32 or (cfg["frames"] - 5) % 17:
                        fail("frame_grid", "Dimensions must align to 32 pixels and frame counts to 17n + 5", node)
                    plan.append({"id": identifier, "node_type": kind, "family": "h3", "operation": "submit", "config": cfg,
                                 "inputs": media, "dependencies": dependencies(media)})
                    value = {"step": identifier}
            cache[key] = copy.deepcopy(value)
            return value
        finally:
            visiting.discard(key)
    chosen = [node for node in document["nodes"] if (node["type"] == "output.video" or registry.get(node["type"],{}).get("output_node")) and node.get("state", "active") == "active"]
    if output_ids is not None:
        if not isinstance(output_ids, list) or any(not isinstance(x, str) for x in output_ids):
            raise ValueError("Output selection must be a list of output node IDs")
        requested = set(output_ids)
        if requested - {node["id"] for node in chosen}:
            report("output_selection", "Choose enabled video output nodes")
        chosen = [node for node in chosen if node["id"] in requested]
    if not chosen:
        report("no_output", "Add and enable a Video output, then connect a result")
    for node in chosen:
        reached.add(node["id"])
        try:
            value = evaluate(node["id"],"result") if registry.get(node["type"],{}).get("execution") == "comfy" else connected(node, "result", required=True)
            if isinstance(value, list):
                plan.append({"id": node["id"], "node_type": "output.video", "family": "media", "operation": "save",
                             "params": _params(node, registry), "inputs": {"video": value}, "dependencies": dependencies({"video": value})})
                source = node["id"]
            else:
                source = value["step"]
            outputs.append({"id": node["id"], "title": node.get("title") or _params(node, registry).get("name") or registry.get(node["type"],{}).get("title",node["type"]), "source": source})
        except _CompileFailure as exc:
            report(exc.code, exc.message, exc.node)
        except (KeyError, TypeError, ValueError) as exc:
            report("invalid_settings", str(exc) if isinstance(exc, ValueError) else "An input connection is incompatible with this operation", node)
    for node in document["nodes"]:
        if node["type"] not in registry and node["id"] not in reached:
            report("unsupported_inactive", "Preserved imported operation; it needs an adapter before connecting it to an enabled output: " + node["type"], node, "warning")
    return {"valid": not any(i["severity"] == "error" for i in issues), "issues": issues, "plan": plan, "outputs": outputs}
