"""Age-specific signed controls survive API, worker, edit and portable workflows."""
from pathlib import Path

import pytest

from scripts import h3_video_worker as worker
from scripts.h3_lora_controls import AGE_SLIDER_LORA
from src import h3_video as h3, video_workflow as workflows
from src.video_job_edit import _draft
from src.video_queue_bulk import _patched
from tests.test_h3_video import config, inventory, weights
from tests.test_video_workflow import transfer


@pytest.fixture
def age_inventory(inventory, tmp_path):
    # The exact adapter must also work outside a repository-named folder.
    weights(tmp_path / "loose-adapters" / AGE_SLIDER_LORA, True)
    weights(tmp_path / "minimax_h3_ref2v_turbo.safetensors", True)
    inventory["components"] = h3.discover_components([tmp_path])
    age = next(item for item in inventory["components"] if item["name"] == AGE_SLIDER_LORA)
    turbo = next(item for item in inventory["components"] if item["name"] == "minimax_h3_ref2v_turbo.safetensors")
    return inventory, age, turbo


def accept(inv, rows):
    return h3.validate_config({**config(inv, "ref2va"), "loras": rows}, inv["components"], inv["gpus"],
                             {"reference_videos": [None]})


def test_age_control_is_reference_only_with_neutral_default(age_inventory):
    inv, age, turbo = age_inventory
    assert age["role"] == "lora" and age["variant"] == "ref2va"
    assert age["strength_control"] == {
        "kind": "age", "label": "Age", "min": -5, "max": 5, "step": .1, "default": 0,
        "labels": {"min": "Youngest", "zero": "Neutral", "max": "Oldest"}}
    assert "strength_control" not in turbo
    with pytest.raises(ValueError, match="REF2VA LoRA"):
        h3.validate_config({**config(inv), "loras": [{"id": age["id"], "strength": -5}]},
                           inv["components"], inv["gpus"], {})


@pytest.mark.parametrize("strength", [-5, 0, 5])
@pytest.mark.parametrize("age_first", [False, True])
def test_signed_age_preserved_through_stack_and_worker(age_inventory, strength, age_first):
    inv, age, turbo = age_inventory
    rows = [{"id": turbo["id"], "strength": 1}, {"id": age["id"], "strength": strength}]
    if age_first:
        rows.reverse()
    saved = accept(inv, rows)
    assert [r["strength"] for r in saved["loras"]] == [r["strength"] for r in rows]
    worker.validate_loras(saved)
    assert [r["strength"] for r in saved["loras"]] == [r["strength"] for r in rows]
    assert saved["lora_scale"] == rows[0]["strength"]
    draft = _draft(saved, inv, "h3")
    assert draft["loras"] == rows
    updated = _patched(saved, {"steps": 4}, inv, "h3", {"reference_videos": [None]})
    assert updated["loras"] == saved["loras"] and updated["steps"] == 4


@pytest.mark.parametrize("strength", [-5, 5])
def test_legacy_age_selection_and_bulk_strength_edit(age_inventory, strength):
    inv, age, _ = age_inventory
    saved = h3.validate_config({**config(inv, "ref2va"), "lora": age["id"], "lora_scale": strength},
                               inv["components"], inv["gpus"], {"reference_videos": [None]})
    assert saved["loras"] == [{"path": age["path"], "strength": strength}]
    updated = _patched(saved, {"lora_scale": -strength}, inv, "h3", {"reference_videos": [None]})
    assert updated["loras"][0]["strength"] == -strength


@pytest.mark.parametrize("strength", [-5.1, 5.1, float("nan"), float("inf"), True])
def test_invalid_age_strength_rejected_before_api_and_worker(age_inventory, strength):
    inv, age, _ = age_inventory
    with pytest.raises(ValueError, match="strength"):
        accept(inv, [{"id": age["id"], "strength": strength}])
    with pytest.raises(ValueError, match="strength"):
        worker.validate_loras({"loras": [{"path": age["path"], "strength": strength}]})


@pytest.mark.parametrize("strength", [-5, 5])
def test_other_loras_keep_existing_limits_even_with_forged_control(age_inventory, strength):
    inv, age, turbo = age_inventory
    turbo["strength_control"] = dict(age["strength_control"])
    with pytest.raises(ValueError, match="-4 to 4"):
        accept(inv, [{"id": age["id"], "strength": 0}, {"id": turbo["id"], "strength": strength}])
    with pytest.raises(ValueError, match="-4 and 4"):
        worker.validate_loras({"loras": [{"path": turbo["path"], "strength": strength}]})


@pytest.mark.parametrize("strength", [-5, 5])
def test_age_portable_roundtrip_keeps_signed_strength(age_inventory, tmp_path, strength):
    inv, age, turbo = age_inventory
    raw = {**config(inv, "ref2va"), "loras": [{"id": turbo["id"], "strength": 1},
                                                {"id": age["id"], "strength": strength}]}
    values, selected = workflows.resolve_draft(raw, inv, "h3")
    document, assets = workflows.make_recipe(values, selected, {}, "h3",
                                             {"include_weights": True, "include_attachments": False})
    assert document["config"]["lora_strengths"] == [1, strength]
    body = b"".join(workflows.stream_zip(document, workflows.open_assets(assets)))
    service, directory = transfer(tmp_path)
    uploaded = directory / "upload.zip"
    uploaded.write_bytes(body)
    imported = service.import_file(directory, "corey", uploaded)
    assert imported["workflow"]["config"]["lora_strengths"] == [1, strength]
    identity = imported["resolved_components"]["lora_1"]
    installed = next(row for row in imported["inventory"]["components"] if row["id"] == identity)
    assert installed["variant"] == "ref2va" and installed["strength_control"]["kind"] == "age"
    assert Path(installed["path"]).read_bytes() == Path(age["path"]).read_bytes()


def test_portable_generic_strength_five_is_rejected(age_inventory):
    _, _, turbo = age_inventory
    document = {"format": workflows.FORMAT, "version": workflows.VERSION, "family": "h3",
                "config": {"mode": "ref2va", "lora_strengths": [5]},
                "components": {"lora_0": {"name": turbo["name"]}}, "attachments": []}
    with pytest.raises(ValueError, match="-4 to 4"):
        workflows.validate_document(document, "h3")
