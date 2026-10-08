"""VFX Edit selection activates its recipe without changing other H3 jobs."""
import json

import pytest

from src import h3_video as h3
from tests.test_h3_video import config, inventory, weights
from tests.test_h3_prompt_enhancement import api, endpoints, upstream, inference, payload


@pytest.fixture
def vfx_inventory(inventory, tmp_path):
    path = weights(tmp_path / "models--Alissonerdx--Minimax-H3-ComfyUI" / "snapshots" / "revision" /
                   "loras" / h3.VFX_EDIT_LORA, True)
    inventory["components"] = h3.discover_components([tmp_path])
    adapter = next(item for item in inventory["components"] if item["name"] == path.name)
    assert adapter["recipe"] == "vfx_edit" and adapter["variant"] == "ref2va"
    raw = {**config(inventory, "ref2va"), "lora": adapter["id"], "frames": 73,
           "prompt": "Make everyone wear a purple jacket. Leave everything else unchanged."}
    return inventory, raw


def validate(bundle, uploads, **changes):
    inv, raw = bundle
    return h3.validate_config({**raw, **changes}, inv["components"], inv["gpus"], uploads)


def test_single_source_vfx_selection_canonicalizes_prefix_without_rewriting(vfx_inventory):
    _, raw = vfx_inventory
    accepted = validate(vfx_inventory, {"reference_videos": [None]})
    assert accepted["prompt"] == "vfx_edit: " + raw["prompt"]
    assert accepted["frames"] == 73
    assert validate(vfx_inventory, {"reference_videos": [None]}, prompt=accepted["prompt"])["prompt"] == accepted["prompt"]


@pytest.mark.parametrize("uploads", [{}, {"reference_videos": [None, None]},
    {"reference_videos": [None], "reference_images": [None]},
    {"reference_videos": [None], "reference_audio": [None]},
    {"reference_videos": [None], "first_frame": [None]}])
def test_vfx_rejects_incompatible_source_combinations(vfx_inventory, uploads):
    with pytest.raises(ValueError, match="exactly one source video"):
        validate(vfx_inventory, uploads)


@pytest.mark.parametrize("prompt", ["Change <Video 1>", "Use <Picture 1>", "Preserve <Subject 1>", "Use <Audio 1>"])
def test_vfx_cannot_mislabel_aligned_guide_as_native_reference(vfx_inventory, prompt):
    with pytest.raises(ValueError, match="aligned guide"):
        validate(vfx_inventory, {"reference_videos": [None]}, prompt=prompt)


def test_short_vfx_frames_do_not_relax_generic_h3_validation(vfx_inventory):
    with pytest.raises(ValueError, match="frames"):
        validate(vfx_inventory, {"reference_videos": [None]}, lora=None)
    with pytest.raises(ValueError, match="REF2VA"):
        inv, raw = vfx_inventory
        h3.validate_config({**config(inv), "lora": raw["lora"]}, inv["components"], inv["gpus"], {})
    with pytest.raises(ValueError, match="prefix"):
        validate(vfx_inventory, {"reference_videos": [None]}, prompt="x" * 16000)


def test_vfx_installed_defaults_validate_source_video_contract(vfx_inventory, tmp_path):
    inv, raw = vfx_inventory
    values = {key: raw[key] for key in ("mode", "model", "encoder", "video_vae", "audio_vae", "lora", "steps")}
    path = tmp_path / "preset.json"
    path.write_text(json.dumps({"format_version": 1, "id": "vfx-edit", "name": "H3 VFX Edit", "config": values}))
    preset, error = h3.installed_h3_preset(path, inv["components"], inv["gpus"], {**h3.DEFAULTS, "gpu": "GPU-blackwell"})
    assert not error and preset["config"]["lora"] == raw["lora"]


def test_manual_vfx_enhancement_uses_edit_contract_and_source_timing(api, endpoints, inference):
    endpoints.add("local")
    inference.state["text"] = "Give everyone a purple jacket while preserving all other details."
    response = api.post("/api/video/h3/enhance-prompt", json=payload(
        mode="ref2va", recipe="vfx_edit", frames=73, reference_counts={"reference_videos": 1}))
    assert response.status_code == 200, response.text
    assert response.json()["prompt"] == "vfx_edit: " + inference.state["text"]
    message = inference.seen[0]["messages"]
    assert "aligned video guide" in message[0]["content"]
    assert "100-250" not in message[0]["content"]
    context = json.loads(message[1]["content"])
    assert context["recipe"] == "vfx_edit" and "duration_seconds" not in context
    assert "Match the source video" in context["timing"]


def test_older_client_can_signal_vfx_enhancement_by_prefix(api, endpoints, inference):
    endpoints.add("local")
    inference.state["text"] = "vfx_edit: Add purple jackets; preserve the scene."
    response = api.post("/api/video/h3/enhance-prompt", json=payload(
        mode="ref2va", prompt="vfx_edit: Add purple jackets", reference_counts={"reference_videos": 1}))
    assert response.status_code == 200
    assert "VFX Edit LoRA" in inference.seen[0]["messages"][0]["content"]


def test_vfx_enhancement_rejects_invented_native_reference(api, endpoints, inference):
    endpoints.add("local")
    inference.state["text"] = "vfx_edit: Change the jackets in <Video 1>."
    response = api.post("/api/video/h3/enhance-prompt", json=payload(
        mode="ref2va", recipe="vfx_edit", reference_counts={"reference_videos": 1}))
    assert response.status_code == 502 and "draft was preserved" in response.text
