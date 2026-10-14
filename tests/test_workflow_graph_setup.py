"""Fresh workflow setup resolves installed assets without rewriting authored work."""
import copy

import pytest

from src.workflow_graph import compile_graph, make_node, new_document
from src.workflow_graph_import import from_native_config
from src.workflow_graph_setup import configure_workflow


def inventory():
    rows = [
        {"id": "ref", "name": "MiniMax_H3_Ref2VA_pruned_nvfp4.safetensors", "role": "model", "variant": "ref2va", "nvfp4": True, "aliases": ["old-ref"]},
        {"id": "full", "name": "MiniMax_H3_Ref2VA_nvfp4_mixed.safetensors", "role": "model", "variant": "ref2va", "nvfp4": True},
        {"id": "fl", "name": "MiniMax_H3_FL2VA_int8.safetensors", "role": "model", "variant": "fl2va"},
        {"id": "singularity", "name": "Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors", "role": "model", "variant": "ref2va"},
        {"id": "encoder", "name": "qwen_minimax_h3_nvfp4.safetensors", "role": "encoder", "nvfp4": True},
        {"id": "video", "name": "minimax_h3_video_vae.safetensors", "role": "video_vae"},
        {"id": "audio", "name": "minimax_h3_audio_vae.safetensors", "role": "audio_vae"},
        {"id": "upscale", "name": "minimax_h3_latent_upscaler.safetensors", "role": "latent_upscaler"},
        {"id": "adapter", "name": "my_authored_lora.safetensors", "role": "lora", "aliases": ["old-adapter"]},
    ]
    return {"h3": {"components": rows, "gpus": [{"id": "blackwell", "name": "RTX 5090", "nvfp4": True}, {"id": "ada", "name": "RTX 4090"}],
                   "defaults": {"model": "ref", "encoder": "encoder", "video_vae": "video", "audio_vae": "audio", "gpu": "blackwell", "lora": "adapter"}}}


def node(doc, identity):
    return next(row for row in doc["nodes"] if row["id"] == identity)


@pytest.mark.parametrize("template,model", [("h3", "fl"), ("seed_hunter", "ref"), ("seed_scout", "ref")])
def test_templates_resolve_full_setup_without_injecting_unrelated_loras(template, model):
    source = new_document(template=template)
    before = copy.deepcopy(source)
    doc, notes = configure_workflow(source, inventory(), template=True)
    assert source == before
    assert node(doc, "model")["params"]["component"] == model
    assert node(doc, "encoder")["params"]["component"] == "encoder"
    assert node(doc, "video_vae")["params"]["component"] == "video"
    assert node(doc, "audio_vae")["params"]["component"] == "audio"
    assert node(doc, "generate")["params"]["gpu"] == "blackwell"
    assert node(doc, "generate")["params"].get("mode") == node(before, "generate")["params"].get("mode")
    assert not any(row["type"] == "model.lora" for row in doc["nodes"])
    assert node(doc, "prompt")["params"].get("text", "") == ""
    assert doc["metadata"]["setup_notes"] == notes
    node(doc, "prompt")["params"]["text"] = "Synthetic setup check."
    assert compile_graph(doc, inventory=inventory())["valid"]
    if template == "seed_hunter":
        assert node(doc, "latent_upscaler")["params"]["component"] == "upscale"
        assert node(doc, "refine")["state"] == "bypassed"


def test_imported_exact_names_aliases_settings_and_adapter_strength_survive():
    source = from_native_config({"mode": "ref2va", "model": "old-ref", "encoder": "encoder", "video_vae": "video", "audio_vae": "audio",
                                 "gpu": "blackwell", "steps": 7, "seed": 987, "prompt": "Authored prompt.", "loras": [{"id": "old-adapter", "strength": -0.6}]})
    doc, _ = configure_workflow(source, inventory())
    assert node(doc, "model")["params"]["component"] == "ref"
    assert node(doc, "lora_0")["params"]["component"] == "adapter"
    assert node(doc, "lora_0")["params"]["strength"] == -0.6
    assert node(doc, "generate")["params"] == node(source, "generate")["params"]
    assert node(doc, "prompt")["params"] == node(source, "prompt")["params"]


def test_named_singularity_is_kept_and_missing_authored_adapter_is_not_replaced():
    source = new_document(template="seed_scout")
    node(source, "model")["params"]["name"] = "C:\\Models\\Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors"
    source["nodes"].append(make_node("authored", "model.lora", params={"name": "not-installed.safetensors", "strength": 0.75, "enabled": False}))
    doc, notes = configure_workflow(source, inventory())
    assert node(doc, "model")["params"]["component"] == "singularity"
    assert node(doc, "authored")["params"] == node(source, "authored")["params"]
    assert any(row["code"] == "adapter_unavailable" for row in notes)


@pytest.mark.parametrize("requested,expected", [("MiniMax_H3_Ref2VA_full_bf16.safetensors", "full"), ("MiniMax_H3_Ref2VA_pruned_bf16.safetensors", "ref")])
def test_missing_base_weight_substitution_keeps_pruning_architecture_and_reports_it(requested, expected):
    source = new_document(template="seed_scout")
    node(source, "model")["params"]["name"] = requested
    doc, notes = configure_workflow(source, inventory())
    assert node(doc, "model")["params"]["component"] == expected
    assert node(doc, "model")["params"]["name"] == requested
    report = next(row for row in notes if row["node_id"] == "model")
    assert report["code"] == "component_substituted" and requested in report["message"]


def test_no_other_variant_is_substituted_when_required_weights_are_unavailable():
    available = inventory()
    available["h3"]["components"] = [row for row in available["h3"]["components"] if row.get("variant") != "ref2va"]
    source = new_document(template="seed_scout")
    doc, notes = configure_workflow(source, available)
    assert node(doc, "model")["params"].get("component", "") == ""
    assert node(doc, "generate")["params"]["mode"] == "ref2va"
    assert any(row["code"] == "component_unavailable" and row["node_id"] == "model" for row in notes)


def test_missing_authored_variant_cannot_silently_switch_to_a_conflicting_mode():
    source = new_document(template="seed_scout")
    node(source, "model")["params"]["name"] = "Missing_MiniMax_H3_FL2VA.safetensors"
    doc, notes = configure_workflow(source, inventory())
    assert not node(doc, "model")["params"].get("component")
    assert any(row["code"] == "model_variant_conflict" for row in notes)


def test_incompatible_gpu_maps_to_blackwell_with_an_explicit_note():
    source = new_document(template="seed_scout")
    node(source, "generate")["params"].update(gpu="ada", vae_gpu="missing-gpu")
    doc, notes = configure_workflow(source, inventory())
    assert node(doc, "generate")["params"]["gpu"] == "blackwell"
    assert node(doc, "generate")["params"]["vae_gpu"] == ""
    assert any(row["code"] == "gpu_substituted" and row["field"] == "gpu" for row in notes)


@pytest.mark.parametrize("metadata", [None, "legacy", []])
def test_non_object_imported_metadata_is_retained_without_blocking_setup(metadata):
    source = new_document(template="seed_scout")
    source["metadata"] = metadata
    doc, notes = configure_workflow(source, inventory())
    assert doc["metadata"]["setup_notes"] == notes
    if metadata is not None:
        assert doc["metadata"]["source_metadata"] == metadata


def test_bfs_roleless_inventory_resolves_required_slots_and_keeps_authored_lora():
    available = inventory()
    available["bfs"] = {"components": [{k: v for k, v in row.items() if k != "role"} for row in available["h3"]["components"]], "gpus": available["h3"]["gpus"]}
    available["bfs"]["components"].append({"id": "head-swap", "name": "minimax_h3_head_swap.safetensors"})
    source = from_native_config({"workflow_id": "h3_head_swap", "components": {}, "prompt": "Swap the face."}, "bfs")
    doc, _ = configure_workflow(source, available)
    assert set(node(doc, "setup")["params"]) >= {"model", "encoder", "video_vae", "audio_vae", "lora"}
    assert node(doc, "setup")["params"]["lora"] == "head-swap"
    node(source, "setup")["params"]["lora"] = "missing-authored-lora"
    doc, notes = configure_workflow(source, available)
    assert node(doc, "setup")["params"]["lora"] == "missing-authored-lora"
    assert any(row["code"] == "adapter_unavailable" for row in notes)
