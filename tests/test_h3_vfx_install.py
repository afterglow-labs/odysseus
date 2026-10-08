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


@pytest.mark.parametrize("uploads", [{"source_video": [None]}, {"reference_videos": [None]}])
def test_single_source_vfx_selection_canonicalizes_prefix_without_rewriting(vfx_inventory, uploads):
    _, raw = vfx_inventory
    accepted = validate(vfx_inventory, uploads)
    assert accepted["prompt"] == "vfx_edit: " + raw["prompt"]
    assert accepted["frames"] == 73
    assert validate(vfx_inventory, uploads, prompt=accepted["prompt"])["prompt"] == accepted["prompt"]


@pytest.mark.parametrize("uploads", [{}, {"reference_videos": [None, None]},
    {"source_video": [], "reference_videos": [None]}, {"source_video": [None, None]},
    {"source_video": [None], "first_frame": [None]},
    {"source_video": [None], "last_frame": [None]}])
def test_vfx_rejects_incompatible_source_combinations(vfx_inventory, uploads):
    with pytest.raises(ValueError, match="source video|keyframe|first.frame|last.frame"):
        validate(vfx_inventory, uploads)


@pytest.mark.parametrize("prompt", ["Use <Video 1> motion", "Use the edited frame in <Picture 1>",
                                    "Preserve <Subject 1> from <Picture 1>", "Use <Audio 1>"])
def test_vfx_accepts_numbered_native_references_separate_from_source(vfx_inventory, prompt):
    accepted = validate(vfx_inventory, {"source_video": [None], "reference_images": [None],
        "reference_videos": [None], "reference_audio": [None]}, prompt=prompt)
    assert accepted["prompt"] == "vfx_edit: " + prompt


@pytest.mark.parametrize("refs", [{"reference_images": [None]}, {"reference_audio": [None]},
                                 {"reference_images": [None], "reference_audio": [None]}])
def test_legacy_source_accepts_additional_native_context(vfx_inventory, refs):
    accepted = validate(vfx_inventory, {"reference_videos": [None], **refs})
    assert accepted["prompt"].startswith("vfx_edit:")


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
    assert context["reference_counts"]["source_video"] == 1
    assert context["reference_counts"]["reference_videos"] == 0


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
