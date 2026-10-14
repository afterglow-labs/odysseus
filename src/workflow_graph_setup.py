"""Resolve new/imported workflow setup against the installation, without running it.

Authored operations, settings, and adapters remain intact. A different installed
base component is an explicit substitution, recorded with the document.
"""
from __future__ import annotations

import copy
import re
from pathlib import PurePosixPath


def _name(value):
    return PurePosixPath(str(value or "").replace("\\", "/")).name


def _identity(row, value):
    return bool(value) and (value == row.get("id") or value in row.get("aliases", [])
                            or value == row.get("path"))


def configure_workflow(document, inventory, *, template=False, add_scout_refinement=False):
    """Return ``(configured copy, setup notes)``; never mutate a saved document.

    Imported compatible GPU/component selections win over installation
    preferences. Missing authored
    adapters are left unresolved rather than replaced with unrelated weights.
    """
    doc = copy.deepcopy(document)
    nodes = {node["id"]: node for node in doc.get("nodes", [])}
    edges = doc.get("edges", [])
    h3 = inventory.get("h3", inventory) or {}
    components = h3.get("components", [])
    gpus = h3.get("gpus", (inventory.get("bfs", {}) or {}).get("gpus", []))
    defaults = h3.get("defaults", {})
    if not isinstance(doc.get("metadata"), dict):
        previous_metadata = doc.get("metadata")
        doc["metadata"] = {"source_metadata": previous_metadata} if previous_metadata is not None else {}
    notes = copy.deepcopy(doc["metadata"].get("setup_notes", []))
    if not isinstance(notes, list):
        notes = []

    def note(node, field, code, message, *, severity="info", **details):
        row = {"node_id": node["id"], "field": field, "code": code,
               "severity": severity, "message": message, **details}
        if row not in notes:
            notes.append(row)

    if add_scout_refinement:
        from src.workflow_graph import make_edge, make_node
        for scout in list(nodes.values()):
            if scout["type"] != "h3.seed_scout" or scout.get("params", {}).get("mode", "t2va") != "ref2va":
                continue
            outgoing = [edge for edge in edges if edge["source"] == {"node": scout["id"], "port": "result"}]
            # Upgrade only the old, direct Scout-to-output template. Authored
            # branching, result selection, and existing refinement stay intact.
            if len(outgoing) != 1 or nodes[outgoing[0]["target"]["node"]]["type"] != "output.video":
                continue
            setup = next((edge["source"] for edge in edges if edge["target"] == {"node": scout["id"], "port": "setup"}
                          and nodes[edge["source"]["node"]]["type"] == "hub.setup"), None)
            if setup is None:
                continue
            identifier = scout["id"] + "_refine"
            while identifier in nodes:
                identifier += "_optional"
            params = scout.get("params", {})
            refine = make_node(identifier, "hunter.refine", x=scout.get("position", {}).get("x", 660) + 300,
                               y=scout.get("position", {}).get("y", 280), state="bypassed",
                               params={"width": params.get("width", 960), "height": params.get("height", 544)})
            # A default 2x target fits the native final-stage pixel limit.
            if refine["params"]["width"] * refine["params"]["height"] * 4 <= 4194304:
                refine["params"]["width"] *= 2; refine["params"]["height"] *= 2
            nodes[identifier] = refine; doc["nodes"].append(refine)
            outgoing[0]["source"] = {"node": identifier, "port": "result"}
            edges.extend([make_edge(scout["id"], "result", identifier, "preview"),
                          make_edge(setup["node"], setup["port"], identifier, "setup")])
            note(refine, "state", "optional_refinement_added", "Added bypassed latent refinement after the completed selected Scout result. Enable it to upscale; existing Scout settings and attachments are preserved.")

    if (template or add_scout_refinement) and any(node["type"] == "hunter.refine" for node in nodes.values()):
        from src.workflow_graph import make_edge, make_node
        for hub in list(nodes.values()):
            if hub["type"] != "hub.setup" or any(edge["target"] == {"node": hub["id"], "port": "latent_upscaler"} for edge in edges):
                continue
            if not any(edge["source"]["node"] == hub["id"] and nodes[edge["target"]["node"]]["type"] == "hunter.refine" for edge in edges):
                continue
            if not any(row.get("role") == "latent_upscaler" for row in components):
                note(hub, "latent_upscaler", "component_unavailable", "Latent refinement requires an installed latent upscaler. Bypassed refinement keeps the original output.", severity="warning")
                continue
            identifier = "latent_upscaler"
            while identifier in nodes:
                identifier += "_setup"
            node = make_node(identifier, "component.latent_upscaler", x=40, y=680)
            nodes[identifier] = node; doc["nodes"].append(node)
            edges.append(make_edge(identifier, "latent_upscaler", hub["id"], "latent_upscaler"))

    def reachable(identifier, direction):
        found, pending = set(), [identifier]
        while pending:
            current = pending.pop()
            for edge in edges:
                source, target = edge["source"]["node"], edge["target"]["node"]
                other = target if direction == "down" else source
                if (source if direction == "down" else target) == current and other not in found:
                    found.add(other); pending.append(other)
        return [nodes[key] for key in found if key in nodes]

    def generation(node):
        return node["type"] in {"h3.generate", "h3.seed_scout"} or node["type"].startswith("hunter.")

    def variants(node):
        result = set()
        for consumer in reachable(node["id"], "down"):
            if not generation(consumer) or consumer.get("state", "active") == "disabled" or consumer["type"] == "hunter.refine":
                continue
            mode = consumer.get("params", {}).get("mode", "t2va")
            result.add("ref2va" if consumer["type"].startswith("hunter.") or mode == "ref2va" else "fl2va")
        return result

    def rank(row, preferred=""):
        # Installation defaults win. Otherwise prefer a supported compact weight
        # format, then a stable name/id ordering rather than scan order.
        compact = 0 if row.get("nvfp4") and any(g.get("nvfp4") for g in gpus) else 1 if "int8" in row.get("name", "").lower() else 2
        return (not _identity(row, preferred), compact, row.get("name", "").casefold(), row.get("id", ""))

    selected_rows = {}
    for node in nodes.values():
        kind = node["type"]
        if not kind.startswith("component.") and kind != "model.lora":
            continue
        role = "lora" if kind == "model.lora" else kind.split(".", 1)[1]
        params = node.setdefault("params", {})
        selected, authored_name = params.get("component", ""), params.get("name", "")
        available = [row for row in components if row.get("role") == role]
        exact = [row for row in available if _identity(row, selected)]
        if not exact and (authored_name or selected):
            requested = _name(authored_name or selected).casefold()
            exact = [row for row in available if _name(row.get("name")).casefold() == requested]
        if len(exact) == 1:
            chosen = exact[0]
            if selected != chosen["id"]:
                params["component"] = chosen["id"]
                note(node, "component", "component_resolved", f"Resolved {role.replace('_', ' ')} to installed {chosen['name']}.",
                     from_value=selected or authored_name, to_value=chosen["id"], to_name=chosen["name"])
            selected_rows[node["id"]] = chosen
            continue
        if len(exact) > 1:
            note(node, "component", "component_ambiguous", f"More than one installed file matches {authored_name or selected}; choose the intended {role.replace('_', ' ')}.", severity="warning")
            continue
        if role == "lora":
            if selected or authored_name or params.get("enabled", True):
                note(node, "component", "adapter_unavailable", f"The authored LoRA {_name(authored_name or selected) or 'selection'} is unavailable; its strength and enabled state were preserved.", severity="warning")
            continue
        required = variants(node)
        if role == "model" and len(required) > 1:
            note(node, "component", "model_variant_conflict", "This shared model feeds both Ref2VA and FL2VA modes; give those branches separate compatible models.", severity="warning")
            continue
        authored_variant = re.search(r"(ref2va|fl2va)", _name(authored_name or selected), re.I) if role == "model" else None
        if authored_variant and required and authored_variant[1].lower() not in required:
            note(node, "component", "model_variant_conflict", "The authored checkpoint variant conflicts with the connected generation mode; its selection was preserved.", severity="warning")
            continue
        choices = [row for row in available if not row.get("nvfp4") or any(g.get("nvfp4") for g in gpus)]
        if role == "model" and required:
            choices = [row for row in choices if row.get("variant") in required]
        if not choices:
            qualifier = next(iter(required)).upper() + " " if role == "model" and required else ""
            note(node, "component", "component_unavailable", f"No compatible installed {qualifier}{role.replace('_', ' ')} is available.", severity="warning")
            continue
        def preference(row):
            requested = _name(authored_name or selected).casefold()
            # Pruned and full checkpoints can have different adapter shapes.
            # Keep that architecture, then the named model family, when the
            # imported filename provides evidence of either.
            architecture = int(("pruned" in requested) != ("pruned" in row.get("name", "").casefold())) if role == "model" and ".safetensors" in requested else 0
            family = int(("singularity" in requested) != ("singularity" in row.get("name", "").casefold())) if role == "model" else 0
            return (architecture, family, *rank(row, defaults.get(role, "")))
        chosen = min(choices, key=preference)
        params["component"] = chosen["id"]
        selected_rows[node["id"]] = chosen
        previous = _name(authored_name or selected)
        note(node, "component", "component_substituted" if previous else "component_selected",
             f"Replaced unavailable {previous} with installed {chosen['name']}." if previous else f"Selected installed {role.replace('_', ' ')}: {chosen['name']}.",
             severity="warning" if previous else "info", from_value=selected or authored_name, to_value=chosen["id"], to_name=chosen["name"])

    # BFS inventory deliberately has no global role: compatibility belongs to a
    # specific workflow slot, including its family and filename pattern.
    bfs = inventory.get("bfs", {}) or {}
    for node in list(nodes.values()):
        if node["type"] != "bfs.components":
            continue
        from src.bfs_video import BY_ID, matches
        consumers = [row for row in reachable(node["id"], "down") if row["type"] == "bfs.generate"]
        workflows = [BY_ID.get(row.get("params", {}).get("workflow_id", "h3_head_swap")) for row in consumers]
        workflows = [row for row in workflows if row]
        params = node.setdefault("params", {})
        for key in {slot["key"] for workflow in workflows for slot in workflow["slots"]}:
            slots = [(slot, workflow) for workflow in workflows for slot in workflow["slots"] if slot["key"] == key]
            selected = params.get(key, "")
            if not selected and not any(slot["required"] for slot, _ in slots):
                continue
            available = bfs.get("components", [])
            exact = [row for row in available if _identity(row, selected)]
            if not exact and selected:
                exact = [row for row in available if _name(row.get("name")).casefold() == _name(selected).casefold()]
            if len(exact) == 1:
                chosen = exact[0]
            else:
                if "lora" in key and selected:
                    note(node, key, "adapter_unavailable", f"The authored {key.replace('_', ' ')} {_name(selected)} is unavailable; it was preserved.", severity="warning")
                    continue
                choices = [row for row in available if all(matches(row, slot, workflow) for slot, workflow in slots)
                           and (not row.get("nvfp4") or any(g.get("nvfp4") for g in bfs.get("gpus", gpus)))]
                if not choices:
                    note(node, key, "component_unavailable", f"No compatible installed BFS {key.replace('_', ' ')} is available.", severity="warning")
                    continue
                preferred = defaults.get(key, "")
                chosen = min(choices, key=lambda row: (not _identity(row, preferred),
                    key == "model" and "singularity" in row.get("name", "").casefold(), *rank(row)))
            if selected != chosen["id"]:
                params[key] = chosen["id"]
                note(node, key, "component_substituted" if selected and not exact else "component_selected",
                     f"Selected installed BFS {key.replace('_', ' ')}: {chosen['name']}." + (f" Replaces unavailable {_name(selected)}." if selected and not exact else ""),
                     severity="warning" if selected and not exact else "info", from_value=selected, to_value=chosen["id"], to_name=chosen["name"])
        if template:
            adapter = next((row for row in bfs.get("components", []) if _identity(row, params.get("lora"))), {})
            for consumer in consumers:
                if consumer.get("params", {}).get("workflow_id") != "ltx2_v1" or "8750" not in adapter.get("name", ""):
                    continue
                if any(edge["target"] == {"node": consumer["id"], "port": "last_frame"} for edge in edges):
                    continue
                from src.workflow_graph import make_edge, make_node
                identifier = consumer["id"] + "_last_frame"
                while identifier in nodes:
                    identifier += "_input"
                frame = make_node(identifier, "input.image", x=40, y=950, title="Last frame")
                nodes[identifier] = frame; doc["nodes"].append(frame)
                edges.append(make_edge(identifier, "image", consumer["id"], "last_frame"))
                note(consumer, "last_frame", "required_input", "The installed first-and-last-frame adapter also requires a last frame.")

    for node in nodes.values():
        if not generation(node) and node["type"] != "bfs.generate":
            continue
        # Refinement has no standalone GPU setting; it follows its preview.
        if node["type"] == "hunter.refine":
            continue
        params = node.setdefault("params", {})
        family_inventory = bfs if node["type"] == "bfs.generate" else h3
        available_gpus = family_inventory.get("gpus", gpus)
        existing = next((gpu for gpu in available_gpus if str(gpu["id"]) == str(params.get("gpu", ""))), None)
        upstream = reachable(node["id"], "up")
        needs_nvfp4 = any(selected_rows.get(row["id"], {}).get("nvfp4") for row in upstream)
        if node["type"] == "bfs.generate":
            chosen_ids = {value for row in upstream if row["type"] == "bfs.components" for value in row.get("params", {}).values() if isinstance(value, str)}
            needs_nvfp4 = any(row.get("id") in chosen_ids and row.get("nvfp4") for row in bfs.get("components", []))
        vae_gpu = params.get("vae_gpu", "")
        if vae_gpu and not any(str(gpu["id"]) == str(vae_gpu) for gpu in available_gpus):
            params["vae_gpu"] = ""
            note(node, "vae_gpu", "gpu_substituted", "The unavailable VAE GPU was replaced with the generation GPU.", from_value=vae_gpu, to_value="")
        if existing and not (needs_nvfp4 and not existing.get("nvfp4")):
            continue
        choices = [gpu for gpu in available_gpus if not needs_nvfp4 or gpu.get("nvfp4")]
        if not choices:
            note(node, "gpu", "gpu_unavailable", "No compatible installed GPU is available for this workflow.", severity="warning")
            continue
        preferred = family_inventory.get("defaults", {}).get("gpu", "")
        chosen = min(choices, key=lambda gpu: (str(gpu["id"]) != str(preferred), not gpu.get("nvfp4"), str(gpu["id"])))
        previous = params.get("gpu", "")
        params["gpu"] = chosen["id"]
        note(node, "gpu", "gpu_substituted" if existing else "gpu_selected",
             f"Selected {chosen.get('name', chosen['id'])} for this workflow." + (" The authored GPU cannot run its NVFP4 weights." if existing else ""),
             severity="warning" if existing else "info", from_value=previous, to_value=chosen["id"])

    doc.setdefault("metadata", {})["setup_notes"] = notes
    return doc, notes
