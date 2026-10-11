"""User-defined adapter profiles remain portable without changing local profiles."""
import json

import pytest

from scripts.h3_lora_controls import read_profile, validate_lora_profile, write_profile
from src import h3_video as h3, video_workflow as workflow
from tests.test_h3_video import weights
from tests.test_video_workflow import archive, document, transfer


def profile(**updates):
    return validate_lora_profile({
        "display_name": "My motion adapter", "trigger_words": "motion_style",
        "trigger_position": "prefix", "notes": "Use a matching reference.", "variant": "ref2va",
        "strength_control": {"kind": "custom", "label": "Motion", "min": -8, "max": 8,
                             "step": .25, "default": 1},
        "recommended_settings": {"steps": 4, "sampler": "euler", "scheduler": "simple",
                                 "shift_video": 12, "shift_audio": 3}, **updates})


def recipe(tmp_path, value=-8):
    path = weights(tmp_path / "source" / "movement.safetensors", True)
    saved = write_profile(path, profile(), overwrite=True)
    components = h3.discover_components([path.parent])
    assert len(components) == 1 and components[0]["variant"] == "ref2va"
    inv = {"components": components}
    config, selected = workflow.resolve_draft({"mode": "ref2va", "prompt": "Keep this prompt",
        "loras": [{"id": components[0]["id"], "strength": value}]}, inv, "h3")
    doc, assets = workflow.make_recipe(config, selected, {}, "h3", workflow.export_options({"include_weights": True}))
    return path, saved, doc, assets


def test_custom_profile_roundtrip_registers_plain_filename_without_modifying_prompt(tmp_path):
    path, saved, doc, assets = recipe(tmp_path)
    assert doc["components"]["lora_0"]["lora_profile"] == saved
    assert doc["components"]["lora_0"]["name"] == "movement.safetensors"
    assert doc["config"]["prompt"] == "Keep this prompt"
    assert doc["config"]["lora_strengths"] == [-8]
    assert str(path.parent) not in json.dumps(doc)
    service, directory = transfer(tmp_path)
    upload = directory / "upload.zip"
    upload.write_bytes(b"".join(workflow.stream_zip(doc, workflow.open_assets(assets))))
    result = service.import_file(directory, "corey", upload)
    identity = result["resolved_components"]["lora_0"]
    imported = next(c for c in result["inventory"]["components"] if c["id"] == identity)
    assert imported["lora_profile"] == saved
    assert imported["strength_control"]["min"] == -8
    assert read_profile(imported["path"]) == saved
    assert result["workflow"]["config"]["lora_strengths"] == [-8]


def test_job_export_uses_saved_profile_after_library_edit(tmp_path):
    path = weights(tmp_path / "minimax_h3_movement.safetensors", True)
    saved = profile()
    write_profile(path, profile(trigger_words="a_new_trigger"), overwrite=True)
    config = {"mode": "ref2va", "prompt": "Saved prompt", "loras": [{"path": str(path), "strength": -8}],
              "lora_profiles": {str(path.resolve()): saved}}
    doc, _ = workflow.make_recipe(config, {}, {}, "h3", workflow.export_options({}))
    assert doc["components"]["lora_0"]["lora_profile"] == saved
    assert doc["config"]["prompt"] == "Saved prompt"


def test_reference_import_keeps_existing_local_profile_and_reports_conflict(tmp_path):
    service, directory = transfer(tmp_path)
    path = weights(service.cache / "minimax_h3_movement.safetensors", True)
    original = write_profile(path, profile(trigger_words="local_trigger"), overwrite=True)
    imported_profile = profile(trigger_words="imported_trigger")
    doc = document(config={"mode": "ref2va", "lora_strengths": [1]}, components={
        "lora_0": {"name": path.name, "lora_profile": imported_profile}})
    source = directory / "workflow.json"
    source.write_text(json.dumps(doc))
    result = service.import_file(directory, "corey", source)
    assert "lora_0" in result["resolved_components"]
    assert read_profile(path) == original
    assert "Kept the existing LoRA profile" in result["warnings"][0]
    assert result["workflow"]["components"]["lora_0"]["lora_profile"] == imported_profile


@pytest.mark.parametrize("change", [
    {"strength_control": {"kind": "custom", "label": "bad", "min": -101, "max": 101, "step": 1, "default": 1}},
    {"recommended_settings": {"execute": "arbitrary code"}},
    {"trigger_position": "execute"},
])
def test_portable_profiles_are_validated_before_any_weight_install(change):
    data = profile()
    data.update(change)
    doc = document(config={"mode": "ref2va", "lora_strengths": [1]}, components={
        "lora_0": {"name": "movement.safetensors", "lora_profile": data}})
    with pytest.raises(ValueError):
        workflow.validate_document(doc, "h3")


def test_profile_cannot_mislabel_base_model_as_lora(tmp_path):
    service, directory = transfer(tmp_path)
    base = weights(tmp_path / "base.safetensors")
    doc = document(config={"mode": "ref2va", "lora_strengths": [1]}, components={
        "lora_0": {"name": base.name, "archive_path": "weights/lora_0/base.safetensors", "lora_profile": profile()}})
    source = archive(directory / "upload.zip", doc, [("weights/lora_0/base.safetensors", base.read_bytes())])
    with pytest.raises(ValueError, match="adapter weights"):
        service.import_file(directory, "corey", source)
    assert not list(service.cache.rglob("*.safetensors"))


def test_unprofiled_adapter_cannot_use_custom_strength():
    doc = document(config={"mode": "ref2va", "lora_strengths": [8]}, components={"lora_0": {"name": "plain.safetensors"}})
    with pytest.raises(ValueError, match="-4 to 4"):
        workflow.validate_document(doc, "h3")


def test_profile_only_allowed_on_lora_component():
    doc = document(components={"model": {"name": "base.safetensors", "lora_profile": profile()}})
    with pytest.raises(ValueError, match="Only H3 LoRA"):
        workflow.validate_document(doc, "h3")


def test_flat_adapter_keeps_nested_hugging_face_reference(tmp_path):
    from src.model_library import write_model_metadata
    path = weights(tmp_path / "movement.safetensors", True)
    write_model_metadata(path.parent, "author/H3-adapters", {
        path.name: {"upstream_relative_path": "loras/movement.safetensors", "revision": "a" * 40}})
    write_profile(path, profile(), overwrite=True)
    doc, _ = workflow.make_recipe({"mode": "ref2va", "loras": [{"path": str(path), "strength": 8}]},
                                 {}, {}, "h3", workflow.export_options({}))
    ref = doc["components"]["lora_0"]
    assert ref["relative_path"] == "loras/movement.safetensors"
    assert ref["repository"] == "author/H3-adapters" and ref["revision"] == "a" * 40
    assert ref["name"] == path.name and ref["lora_profile"] == profile()


@pytest.mark.parametrize("bundled", [False, True])
def test_import_invalidates_warm_submission_inventory_before_immediate_queue(tmp_path, bundled):
    from src.h3_loras import lora_entries
    service, directory = transfer(tmp_path)
    if bundled:
        _, _, doc, assets = recipe(tmp_path, value=8)
        source = directory / "upload.zip"
        source.write_bytes(b"".join(workflow.stream_zip(doc, workflow.open_assets(assets))))
    else:
        installed = weights(service.cache / "minimax_h3_movement.safetensors", True)
        doc = document(config={"mode": "ref2va", "lora_strengths": [8]}, components={
            "lora_0": {"name": installed.name, "lora_profile": profile()}})
        source = directory / "workflow.json"
        source.write_text(json.dumps(doc))
    previous = service.manager.submission_inventory()
    assert not any(item.get("lora_profile") for item in previous["components"])
    imported = service.import_file(directory, "corey", source)
    refreshed = service.manager.submission_inventory()
    identity = imported["resolved_components"]["lora_0"]
    components = {item["id"]: item for item in refreshed["components"]}
    assert refreshed is not previous and identity in components
    assert components[identity]["strength_control"]["max"] == 8
    assert lora_entries({"loras": [{"id": identity, "strength": 8}]}, components=components)
