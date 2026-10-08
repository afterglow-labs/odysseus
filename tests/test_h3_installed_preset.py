"""Installed Turbo presets affect fresh drafts, never saved job configuration."""
import copy
import json
from pathlib import Path

import pytest

from src import h3_video as h3
from tests.test_h3_video import config, inventory, weights


@pytest.fixture
def installation(tmp_path, inventory):
    root = tmp_path / "models--lightx2v--Minimax-h3-Turbo" / "snapshots" / "revision"
    for mode in ("ref2v", "fl2v"):
        weights(root / f"minimax_h3_{mode}_turbo_8step_v1.0_768p_comfyui_bf16.safetensors", True)
    inventory["components"] = h3.discover_components([tmp_path])
    values = config(inventory, "ref2va")
    values = {key: value for key, value in values.items() if key in {
        "mode", "model", "encoder", "video_vae", "audio_vae", "steps", "sampler", "scheduler", "shift_video", "shift_audio", "lora_scale"}}
    lora = next(item for item in inventory["components"] if item["role"] == "lora" and item["variant"] == "ref2va")
    values.update(lora=lora["path"], steps=8)
    document = {"format_version": 1, "id": "lightx2v-ref2v-turbo-v1", "name": "H3 Ref2V Turbo · 8 steps", "config": values}
    path = tmp_path / "data" / "h3_video_defaults.json"
    path.parent.mkdir()
    path.write_text(json.dumps(document))
    return path, document, inventory


def read_preset(installation):
    path, _, inv = installation
    defaults = {**h3.DEFAULTS, "gpu": "GPU-blackwell"}
    return h3.installed_h3_preset(path, inv["components"], inv["gpus"], defaults)


def test_turbo_filenames_discover_mode_and_path_defaults_resolve_to_ids(installation):
    _, document, inv = installation
    preset, error = read_preset(installation)
    assert not error
    assert preset["config"]["mode"] == "ref2va"
    assert preset["config"]["steps"] == 8
    assert preset["config"]["lora"] != document["config"]["lora"]
    assert len(preset["config"]["lora"]) == 32
    assert {item["variant"] for item in inv["components"] if "turbo" in item["name"]} == {"fl2va", "ref2va"}
    repeated, _ = read_preset(installation)
    assert repeated["revision"] == preset["revision"]


def test_inventory_publishes_complete_native_defaults_and_keeps_base_defaults(installation, monkeypatch, tmp_path):
    path, _, inv = installation
    monkeypatch.setattr(h3, "discover_components", lambda roots: inv["components"])
    monkeypatch.setattr(h3, "gpu_inventory", lambda: inv["gpus"])
    monkeypatch.setattr(h3, "runtime_error", lambda *args: "")
    manager = h3.H3JobManager(root=tmp_path / "jobs", preset_file=path)
    actual = manager.inventory()
    assert actual["defaults"]["mode"] == "ref2va"
    assert actual["defaults"]["steps"] == 8
    assert actual["base_defaults"]["mode"] == "t2va"
    assert actual["base_defaults"]["steps"] == 20
    # Mirrors native iOS: defaults -> chooseMode -> submit with references.
    h3.validate_config({**actual["defaults"], "prompt": "Use <Video 1>"}, actual["components"], actual["gpus"], {"reference_videos": [None]})
    # The global defaults and existing 20-step jobs remain independent.
    assert h3.DEFAULTS["steps"] == 20
    existing = h3.validate_config(config(inv), inv["components"], inv["gpus"], {})
    assert existing["steps"] == 20 and existing["lora"] is None
    assert not manager.root.exists()


@pytest.mark.parametrize("change", [
    {"lora": "/missing/turbo.safetensors"}, {"steps": 0}, {"steps": True},
    {"sampler": "made_up"}, {"scheduler": "made_up"}, {"shift_audio": float("nan")},
    {"gpu": "GPU-ada"}, {"prompt": "Do not inject into drafts"}, {"width": 640},
    {"mode": "t2va"}, {"lora_scale": float("inf")}, {"lora": None},
])
def test_missing_or_invalid_installations_never_partially_apply(installation, change):
    path, document, _ = installation
    document["config"].update(change)
    path.write_text(json.dumps(document))
    preset, error = read_preset(installation)
    assert preset is None and "unavailable" in error


def test_mismatched_lora_fails_before_worker_for_native_mode_change_and_bulk_edits(installation):
    _, _, inv = installation
    ref_lora = next(item for item in inv["components"] if item["role"] == "lora" and item["variant"] == "ref2va")
    with pytest.raises(ValueError, match="REF2VA LoRA"):
        h3.validate_config({**config(inv), "lora": ref_lora["id"]}, inv["components"], inv["gpus"], {})
    fl_lora = next(item for item in inv["components"] if item["role"] == "lora" and item["variant"] == "fl2va")
    with pytest.raises(ValueError, match="FL2VA LoRA"):
        h3.validate_config({**config(inv, "ref2va"), "lora": fl_lora["id"]}, inv["components"], inv["gpus"], {"reference_images": [None]})
    shared = next(item for item in inv["components"] if item["role"] == "lora" and item["variant"] == "shared")
    accepted = h3.validate_config({**config(inv, "ref2va"), "lora": shared["id"]}, inv["components"], inv["gpus"], {"reference_images": [None]})
    assert accepted["lora"] == shared["path"]


def test_missing_preset_is_optional_and_changed_settings_change_revision(installation):
    path, document, _ = installation
    before, _ = read_preset(installation)
    document["config"]["lora_scale"] = 0.8
    path.write_text(json.dumps(document))
    after, _ = read_preset(installation)
    assert before["revision"] != after["revision"]
    path.unlink()
    assert read_preset(installation) == (None, "")


def test_wrong_role_or_unrecognized_document_is_not_accepted(installation):
    path, document, inv = installation
    wrong_role = copy.deepcopy(document)
    wrong_role["config"]["lora"] = wrong_role["config"]["video_vae"]
    path.write_text(json.dumps(wrong_role))
    assert read_preset(installation)[0] is None
    document["run_jobs"] = True
    path.write_text(json.dumps(document))
    assert read_preset(installation)[0] is None
