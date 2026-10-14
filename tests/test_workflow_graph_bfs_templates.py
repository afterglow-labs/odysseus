"""Every BFS launch family remains available as a configured editable graph."""
from pathlib import Path

import pytest

from src.bfs_video import BY_ID, validate_config
from src.workflow_graph import catalog, compile_graph, new_document, validate_document
from src.workflow_graph_setup import configure_workflow


@pytest.fixture
def bfs_inventory(tmp_path):
    names = [
        "minimax_h3_ref2va_int8.safetensors", "qwen_minimax_h3.safetensors",
        "minimax_h3_video_vae.safetensors", "minimax_h3_audio_vae.safetensors", "minimax_h3_head_swap.safetensors",
        "ltx-2-dev.safetensors", "gemma_3_12b.safetensors", "ltx-2-video_vae.safetensors",
        "ltx-2-audio_vae.safetensors", "ltx-2-text_projection.safetensors",
        "head_swap_v1_13500_first_frame.safetensors", "head_swap_v2_multimodes.safetensors",
        "ltx-2.3-distilled.safetensors", "ltx-2.3-video_vae.safetensors", "ltx-2.3-audio_vae.safetensors",
        "ltx-2.3-text_projection.safetensors", "head_swap_v3_rank_64.safetensors",
        "ltx-2.5-distilled.safetensors", "gemma_4_12b_ltx_2_5.safetensors", "ltx-2.5-video_vae.safetensors",
        "ltx-2.5-audio_vae.safetensors", "head_swap_ltx25_r128_v1.safetensors", "head_swap_ltx25_r128_v1.1.safetensors",
        "wan_bernini_high_noise.safetensors", "wan_bernini_low_noise.safetensors", "umt5_xxl.safetensors",
        "wan_2.1_vae.safetensors", "headswap_bernini_r64_high_noise.safetensors", "headswap_bernini_r64_low_noise.safetensors",
    ]
    components = []
    for index, name in enumerate(names):
        path = tmp_path / name
        path.write_bytes(b"synthetic cached component")
        components.append({"id": str(index), "name": name, "path": str(path), "nvfp4": False})
    return {"components": components, "gpus": [{"id": "test-gpu", "name": "Test GPU", "nvfp4": True}],
            "defaults": {"gpu": "test-gpu"}, "runtime_ready": True, "runtime_error": ""}


def test_new_workflow_catalog_keeps_every_previous_bfs_family():
    templates = {row["id"]: row for row in catalog()["templates"]}
    assert {"bfs_" + identity for identity in BY_ID} <= templates.keys()
    assert "mask" in templates["bfs_ltx2_v2"]["description"].lower()
    for template in templates:
        validate_document(new_document(template=template))


@pytest.mark.parametrize("identity", list(BY_ID))
def test_bfs_templates_preselect_valid_family_assets_and_required_media(identity, bfs_inventory):
    original = new_document(template="bfs_" + identity)
    doc, notes = configure_workflow(original, {"bfs": bfs_inventory}, template=True)
    assert not [row for row in notes if row["severity"] == "warning"]
    compiled = compile_graph(doc, inventory={"bfs": bfs_inventory})
    assert compiled["valid"], compiled["issues"]
    assert len(compiled["plan"]) == 1
    step = compiled["plan"][0]
    expected_inputs = {key for key, requirement in BY_ID[identity]["source_requirements"].items()
                       if isinstance(requirement, dict) and requirement.get("required")}
    assert set(step["inputs"]) == expected_inputs
    assert step["family"] == "bfs" and step["config"]["workflow_id"] == identity
    uploads = {key: [None] for key in expected_inputs}
    checked = validate_config(step["config"], bfs_inventory, uploads)
    assert checked["frames"] == BY_ID[identity]["defaults"]["frames"]
    assert checked["steps"] == BY_ID[identity]["defaults"]["steps"]
    assert checked["lora_scale"] == BY_ID[identity]["defaults"]["lora_scale"]
    assert Path(checked["model"]).name.startswith(("minimax", "ltx", "wan"))
    assert not any(node["type"] == "input.references" for node in doc["nodes"])


def test_unknown_bfs_template_does_not_fall_back_to_another_family():
    with pytest.raises(ValueError, match="Unknown BFS"):
        new_document(template="bfs_not-installed")


def test_first_and_last_adapter_gets_its_required_input_when_it_is_the_installed_choice(bfs_inventory, tmp_path):
    bfs_inventory["components"] = [row for row in bfs_inventory["components"] if "13500" not in row["name"]]
    path = tmp_path / "head_swap_v1_8750_first_and_last_frame.safetensors"
    path.write_bytes(b"synthetic cached adapter")
    bfs_inventory["components"].append({"id": "first-last", "name": path.name, "path": str(path), "nvfp4": False})
    doc, _ = configure_workflow(new_document(template="bfs_ltx2_v1"), {"bfs": bfs_inventory}, template=True)
    compiled = compile_graph(doc, inventory={"bfs": bfs_inventory})
    assert compiled["valid"]
    step = compiled["plan"][0]
    assert set(step["inputs"]) == {"source_video", "identity_image", "last_frame"}
    validate_config(step["config"], bfs_inventory, {key: [None] for key in step["inputs"]})
