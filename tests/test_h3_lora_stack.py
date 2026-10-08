"""Ordered adapters survive validation, queue edits, and portable transfers."""
import json
from pathlib import Path

import pytest

from src import h3_video as h3, video_workflow as workflows
from src.video_job_edit import _draft
from src.video_queue_bulk import _patched
from tests.test_h3_video import config, inventory, weights
from tests.test_video_workflow import transfer


@pytest.fixture
def stack(inventory, tmp_path):
    root = tmp_path / "models--author--MiniMax-H3" / "snapshots" / "revision"
    weights(root / "minimax_h3_ref2v_turbo.safetensors", True)
    weights(root / h3.VFX_EDIT_LORA, True)
    inventory["components"] = h3.discover_components([tmp_path])
    ids = {item["name"]: item["id"] for item in inventory["components"]}
    rows = [{"id": ids["minimax_h3_ref2v_turbo.safetensors"], "strength": 0.6},
            {"id": ids[h3.VFX_EDIT_LORA], "strength": 1.1}]
    raw = {**config(inventory, "ref2va"), "loras": rows,
           "prompt": "Make jackets purple. Preserve everything else.", "frames": 73}
    return inventory, raw


def accept(stack, uploads=None, **changes):
    inv, raw = stack
    return h3.validate_config({**raw, **changes}, inv["components"], inv["gpus"],
                             {"source_video": [None]} if uploads is None else uploads)


def test_stack_uses_each_strength_and_vfx_in_second_position(stack):
    result = accept(stack)
    assert [Path(row["path"]).name for row in result["loras"]] == ["minimax_h3_ref2v_turbo.safetensors", h3.VFX_EDIT_LORA]
    assert [row["strength"] for row in result["loras"]] == [0.6, 1.1]
    assert result["prompt"].startswith("vfx_edit: ")
    assert result["lora"] == result["loras"][0]["path"]


def test_explicit_empty_stack_clears_legacy_adapter(stack):
    inv, raw = stack
    result = accept(stack, uploads={"reference_videos": [None]}, loras=[], frames=124,
                    lora=raw["loras"][1]["id"], lora_scale=1)
    assert result["loras"] == [] and result["lora"] is None
    assert not result["prompt"].startswith("vfx_edit:")


@pytest.mark.parametrize("strength", [float("nan"), float("inf"), True, 5, "1"])
def test_bad_individual_strength_rejected(stack, strength):
    _, raw = stack
    with pytest.raises(ValueError, match="strength"):
        accept(stack, loras=[{**raw["loras"][0], "strength": strength}])


def test_missing_duplicate_and_wrong_role_fail_before_queue(stack):
    _, raw = stack
    for rows in ([raw["loras"][0]] * 2, [{"id": "missing", "strength": 1}],
                 [{"id": raw["model"], "strength": 1}], [raw["loras"][0]] * 9):
        with pytest.raises(ValueError):
            accept(stack, loras=rows)


def test_zero_strength_vfx_does_not_activate_guide_recipe(stack):
    _, raw = stack
    result = accept(stack, uploads={"reference_videos": [None]}, frames=124,
                    loras=[raw["loras"][0], {**raw["loras"][1], "strength": 0}])
    assert not result["prompt"].startswith("vfx_edit:")
    assert result["loras"][1]["strength"] == 0


def test_every_active_adapter_is_checked_for_mode_compatibility(stack):
    inv, raw = stack
    active = [{**row, "strength": 1} for row in raw["loras"]]
    with pytest.raises(ValueError, match="REF2VA LoRA"):
        h3.validate_config({**config(inv), "loras": active}, inv["components"], inv["gpus"], {})


def test_queue_draft_bulk_edit_and_rerun_keep_stack(stack):
    inv, raw = stack
    saved = accept(stack)
    draft = _draft(saved, inv, "h3")
    assert draft["loras"] == raw["loras"]
    assert all("/" not in row["id"] for row in draft["loras"])
    inputs = {"source_video": [None], "reference_images": [None], "reference_videos": [None]}
    patched = _patched(saved, {"steps": 8}, inv, "h3", inputs)
    assert patched["loras"] == saved["loras"] and patched["steps"] == 8
    replacement = [{**raw["loras"][1], "strength": 0.7}]
    assert _patched(saved, {"loras": replacement}, inv, "h3", inputs)["loras"] == [
        {"path": saved["loras"][1]["path"], "strength": 0.7}]


def test_legacy_strength_edit_keeps_other_adapters(stack):
    inv, _ = stack
    saved = accept(stack)
    updated = _patched(saved, {"lora_scale": 0.9}, inv, "h3", {"source_video": [None]})
    assert [row["strength"] for row in updated["loras"]] == [0.9, 1.1]
    assert [row["strength"] for row in saved["loras"]] == [0.6, 1.1]


def test_all_weights_and_strengths_roundtrip_in_one_workflow(stack, tmp_path):
    inv, raw = stack
    config_values, selected = workflows.resolve_draft(raw, inv, "h3")
    doc, assets = workflows.make_recipe(config_values, selected, {}, "h3", {"include_weights": True, "include_attachments": False})
    assert doc["config"]["lora_strengths"] == [0.6, 1.1]
    assert doc["components"]["lora_1"]["name"] == h3.VFX_EDIT_LORA
    assert "lora" not in doc["components"]
    body = b"".join(workflows.stream_zip(doc, workflows.open_assets(assets)))
    assert str(tmp_path).encode() not in body
    service, directory = transfer(tmp_path)
    uploaded = directory / "upload.zip"
    uploaded.write_bytes(body)
    imported = service.import_file(directory, "corey", uploaded)
    assert imported["workflow"]["config"]["lora_strengths"] == [0.6, 1.1]
    for index, source in enumerate(accept(stack)["loras"]):
        identity = imported["resolved_components"][f"lora_{index}"]
        target = next(item for item in imported["inventory"]["components"] if item["id"] == identity)
        assert Path(target["path"]).read_bytes() == Path(source["path"]).read_bytes()


def test_export_saved_manifest_includes_every_adapter(stack):
    inv, _ = stack
    saved = accept(stack)
    selected = {role: saved[role] for role in ("model", "encoder", "video_vae", "audio_vae", "lora")}
    doc, assets = workflows.make_recipe(saved, selected, {}, "h3", {"include_weights": False, "include_attachments": False})
    assert set(doc["components"]) == {"model", "encoder", "video_vae", "audio_vae", "lora_0", "lora_1"}
    assert doc["config"]["lora_strengths"] == [0.6, 1.1] and not assets


def test_malformed_portable_stack_is_rejected(stack):
    inv, raw = stack
    values, selected = workflows.resolve_draft(raw, inv, "h3")
    doc, _ = workflows.make_recipe(values, selected, {}, "h3", {"include_weights": False, "include_attachments": False})
    doc["components"].pop("lora_1")
    with pytest.raises(ValueError, match="missing"):
        workflows.validate_document(doc, "h3")
    doc["config"]["lora_strengths"] = [True]
    with pytest.raises(ValueError, match="strengths"):
        workflows.validate_document(doc, "h3")
