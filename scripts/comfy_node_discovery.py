#!/usr/bin/env python3
"""CPU-only, one-shot package metadata worker; never start a Comfy web server."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import hashlib
import inspect
import json
from pathlib import Path
import re
import sys
import types

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from src.comfy_node_manifest import FORMAT, MAX_MANIFEST_BYTES, _json_safe, normalize_node_info


def problem(code, message, error=None, **details):
    # Do not emit arbitrary exception messages: packages sometimes put credentials
    # or full configuration values in exception text or their import logs.
    result = {"code": code, "message": message, **details}
    if error is not None:
        result["exception_type"] = type(error).__name__
        missing = getattr(error, "name", None) if isinstance(error, ModuleNotFoundError) else None
        if isinstance(missing, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", missing):
            result["missing_module"] = missing
    return result


def hook_info(cls, name):
    value = getattr(cls, name, None)
    result = {"present": callable(value)}
    if callable(value):
        try:
            signature = inspect.signature(value)
            result["parameters"] = [{"name": p.name, "kind": p.kind.name,
                                     "required": p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}
                                    for p in signature.parameters.values()]
        except (TypeError, ValueError):
            result["parameters"] = None
    return result


def configure_runtime(runtime, model_paths):
    sys.path.insert(0, str(runtime))
    sys.argv = ["odysseus-comfy-node-discovery", "--cpu"]
    diagnostics = {"cpu_only": True, "folder_paths_available": False,
                   "server_available": bool((runtime / "server.py").is_file()), "server_started": False}
    if (runtime / "comfy/cli_args.py").is_file():
        import comfy.options
        comfy.options.enable_args_parsing()
        from comfy.cli_args import args
        args.cpu = True  # Also explicit if this runtime disables argument parsing.
    if (runtime / "folder_paths.py").is_file():
        import folder_paths
        diagnostics["folder_paths_available"] = True
        # These affect only this short-lived CPU discovery interpreter.
        for category, directories in model_paths.items():
            for directory in directories:
                folder_paths.add_model_folder_path(category, directory)
        try:
            from aiohttp import web
            import server
            # Route declarations at package import time can be inspected without
            # constructing PromptServer or listening on any network interface.
            if getattr(server.PromptServer, "instance", None) is None:
                server.PromptServer.instance = types.SimpleNamespace(routes=web.RouteTableDef(), client_id=None,
                                                                     send_sync=lambda *_a, **_k: None)
            diagnostics["server_imported"] = True
            diagnostics["route_context"] = "metadata_only"
        except Exception as error:
            diagnostics["server_imported"] = False
            diagnostics["server_error_type"] = type(error).__name__
    return diagnostics


def load_package(path):
    package_root = path if path.is_dir() else path.parent
    if path.is_dir():
        if (path / "__init__.py").is_file():
            entry = path / "__init__.py"
        elif (path / "nodes.py").is_file():
            entry = path / "nodes.py"
        else:
            raise ValueError("A package entrypoint is required")
    else:
        entry = path
    name = "_odysseus_discovered_package_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    if entry.name == "__init__.py":
        spec = importlib.util.spec_from_file_location(name, entry, submodule_search_locations=[str(package_root)])
    else:
        # A namespace wrapper lets file entrypoints use relative sibling imports
        # without shadowing Comfy's own top-level `nodes` module.
        parent = types.ModuleType(name)
        parent.__path__ = [str(package_root)]
        sys.modules[name] = parent
        spec = importlib.util.spec_from_file_location(name + "." + entry.stem, entry)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load this Python entrypoint")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, entry


async def node_mapping(module):
    mapping = getattr(module, "NODE_CLASS_MAPPINGS", None)
    if mapping is not None:
        if not isinstance(mapping, dict):
            raise ValueError("NODE_CLASS_MAPPINGS must be an object")
        return list(mapping.items()), "legacy"
    entrypoint = getattr(module, "comfy_entrypoint", None)
    if not callable(entrypoint):
        raise ValueError("No node registry entrypoint")
    extension = entrypoint()
    if inspect.isawaitable(extension):
        extension = await extension
    # Match Comfy's extension registration lifecycle, in this isolated process.
    on_load = getattr(extension, "on_load", None)
    if callable(on_load):
        result = on_load()
        if inspect.isawaitable(result):
            await result
    getter = getattr(extension, "get_node_list", None)
    if not callable(getter):
        raise ValueError("The V3 extension has no node list")
    classes = getter()
    if inspect.isawaitable(classes):
        classes = await classes
    if not isinstance(classes, (tuple, list)):
        raise ValueError("The V3 extension must return a node list")
    result = []
    for cls in classes:
        try:
            schema = cls.GET_SCHEMA()
            result.append((schema.node_id, cls))
        except Exception as error:
            result.append((f"invalid_schema_{len(result)}", error))
    return result, "v3"


def discover(path, runtime, model_paths):
    result = {"format": FORMAT, "version": 1, "execution_status": "metadata_only", "status": "failed", "nodes": [], "issues": []}
    try:
        result["discovery"] = configure_runtime(runtime, model_paths)
        module, entry = load_package(path)
        result["entrypoint"] = entry.relative_to(path if path.is_dir() else path.parent).as_posix()
        mappings, api = asyncio.run(node_mapping(module))
        result["api"] = api
    except Exception as error:
        result["issues"].append(problem("package_import", "The package could not be imported for metadata discovery", error))
        return result
    display_names = getattr(module, "NODE_DISPLAY_NAME_MAPPINGS", {})
    if not isinstance(display_names, dict):
        display_names = {}
    result["node_display_name_mappings"] = _json_safe(display_names)
    seen = set()
    for class_type, cls in mappings:
        try:
            if not isinstance(class_type, str) or not class_type or len(class_type) > 240:
                raise ValueError("Invalid node class ID")
            if class_type in seen:
                result["nodes"] = [n for n in result["nodes"] if n["class_type"] != class_type]
                result["issues"].append(problem("class_collision", "Duplicate node class IDs cannot be resolved safely", class_type=class_type))
                continue
            seen.add(class_type)
            if isinstance(cls, Exception):
                raise cls
            if not inspect.isclass(cls):
                raise ValueError("The mapping value is not a node class")
            if callable(getattr(cls, "GET_NODE_INFO_V1", None)):
                info = cls.GET_NODE_INFO_V1()
                node_api = "v3"
            else:
                inputs = cls.INPUT_TYPES()
                returns = getattr(cls, "RETURN_TYPES", ())
                info = {"input": inputs, "input_order": {section: list(fields) for section, fields in inputs.items()},
                        "output": returns, "output_name": getattr(cls, "RETURN_NAMES", returns),
                        "output_is_list": getattr(cls, "OUTPUT_IS_LIST", [False] * len(returns)),
                        "is_input_list": getattr(cls, "INPUT_IS_LIST", False),
                        "output_node": getattr(cls, "OUTPUT_NODE", False), "category": getattr(cls, "CATEGORY", "ComfyUI"),
                        "description": getattr(cls, "DESCRIPTION", ""), "deprecated": getattr(cls, "DEPRECATED", False),
                        "experimental": getattr(cls, "EXPERIMENTAL", False),
                        "has_intermediate_output": getattr(cls, "HAS_INTERMEDIATE_OUTPUT", False)}
                node_api = "legacy"
            info["display_name"] = display_names.get(class_type, info.get("display_name", class_type))
            function = getattr(cls, "FUNCTION", "execute" if node_api == "v3" else None)
            hooks = {"validate_inputs": hook_info(cls, "VALIDATE_INPUTS"), "is_changed": hook_info(cls, "IS_CHANGED")}
            row = normalize_node_info(class_type, info, function=function, hooks=hooks)
            row["api"] = node_api
            row["implementation"] = {"module": cls.__module__, "class": cls.__qualname__, "function_present": callable(getattr(cls, function, None)) if isinstance(function, str) else False}
            result["nodes"].append(row)
        except Exception as error:
            result["issues"].append(problem("node_schema", "This node class could not provide its metadata", error, class_type=str(class_type)[:240]))
    result["status"] = "partial" if result["nodes"] and result["issues"] else "ready" if result["nodes"] else "failed"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-paths", type=Path)
    args = parser.parse_args()
    output = args.output
    paths = json.loads(args.model_paths.read_text()) if args.model_paths else {}
    result = discover(args.package.resolve(), args.runtime.resolve(), paths)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_MANIFEST_BYTES:
        encoded = json.dumps({"format": FORMAT, "version": 1, "status": "failed", "execution_status": "metadata_only", "nodes": [],
                              "issues": [problem("manifest_size", "Package metadata exceeded the manifest size limit")]}).encode()
    output.write_bytes(encoded)


if __name__ == "__main__":
    main()
