from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import h3md_controls as controls
from scripts.h3md_worker import make_plan
from scripts.h3_video_worker import validate_job
from src import h3md, h3_video as h3
from src.video_job_listing import list_options
from src.video_history_delete import _matches
from src.video_job_edit import _draft
from tests.test_h3_video import inventory as base_inventory, config, weights


@pytest.fixture
def inventory(base_inventory, tmp_path):
    for role, name in controls.COMPONENTS.items():
        weights(tmp_path / name)
    for name, low, high in controls.LORAS:
        if name != controls.LORAS[5][0]:
            weights(tmp_path / name, lora=True)
    base_inventory["components"] = h3.discover_components([tmp_path])
    return base_inventory


def draft(inventory):
    return {**config(inventory, "ref2va"), **h3md.capabilities(Path("/absent"), inventory["components"])["defaults"]}


def test_two_pass_defaults_select_both_loras_and_heretic(inventory):
    raw = draft(inventory)
    resolved = h3.validate_config(raw, inventory["components"], inventory["gpus"], {})
    assert resolved["recipe"] == "h3md"
    assert "heretic" in resolved["encoder"]
    assert [Path(l["path"]).name for l in resolved["loras"]] == [l[0] for l in controls.LORAS[:2]]
    assert [l["strength"] for l in resolved["loras"]] == [1, 1]
    assert resolved["h3md"]["high_res_strengths"] == [1, 1]
    assert resolved["steps"] == 8 and resolved["h3md"]["transition_step"] == 6
    assert Path(resolved["latent_upscaler"]).name == controls.UPSCALER
    result, media = validate_job({"config": {**resolved, "gpu": "0", "vae_gpu": ""}, "uploads": {}, "output_path": "/tmp/result.mp4"})
    assert result["h3md"] == resolved["h3md"]
    assert _draft(resolved, inventory, "h3")["loras"] == raw["loras"]


def test_author_preset_keeps_missing_enhancer_visible(inventory):
    cap = h3md.capabilities(Path("/absent"), inventory["components"])
    core, source = cap["presets"]
    assert core["ready"]
    assert not source["ready"] and source["missing"] == [controls.LORAS[5][0]]
    assert len(source["config"]["loras"]) == 8
    assert source["config"]["h3md"]["high_res_strengths"] == [r[2] for r in controls.LORAS]


@pytest.mark.parametrize("patch", [{"transition_step": 8}, {"high_res_strengths": [1]},
    {"shots": []}, {"rerun_segments": "../x"}, {"shots": [{"id": "a", "frames": 158, "overlap": 39}]},
    {"rho": float("nan")}, {"continue_audio": 1}, {"cache_dir": "/tmp/elsewhere"}])
def test_invalid_options_fail_before_inference(inventory, patch):
    raw = draft(inventory)
    raw["h3md"].update(patch)
    with pytest.raises(ValueError):
        h3.validate_config(raw, inventory["components"], inventory["gpus"], {})


def test_shots_use_stable_owned_media_keys_and_explicit_locked_audio(inventory):
    raw = draft(inventory)
    raw["h3md"]["shots"][0]["audios"] = [{"index": 0, "mode": "locked", "offset": 4}]
    plan, paths = make_plan(raw, {"reference_images": ["/job/new-file.png"], "reference_audio": ["/job/music.wav"]})
    assert plan["segments"][0]["images"] == ["images-0"]
    assert plan["segments"][0]["audios"] == [{"id": "audios-0", "mode": "locked", "offset": 4}]
    assert paths == {"images-0": "/job/new-file.png", "audios-0": "/job/music.wav"}
    assert plan["media"]["images"][0]["file"] == "images-0"


def test_resume_cache_is_independent_and_rejects_links(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    cache = source / "h3md-cache" / "resume-1"
    cache.mkdir(parents=True)
    (cache / "seg_001.json").write_text("old")
    h3md.clone_cache(source, target)
    (target / "h3md-cache/resume-1/seg_001.json").write_text("new")
    assert (cache / "seg_001.json").read_text() == "old"
    (cache / "outside").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        h3md.clone_cache(source, tmp_path / "third")


def test_resume_rejects_changed_lora_weights(inventory):
    original = draft(inventory)
    changed = deepcopy(original)
    changed["h3md"]["resume_from_cache"] = True
    changed["h3md"]["high_res_strengths"][0] = .7
    with pytest.raises(ValueError, match="strengths"):
        h3md.guard_edit(original, changed)


def test_history_is_separate():
    assert list_options({"recipe": "h3md"}, "h3")["recipe"] == "h3md"
    manager = SimpleNamespace(root=Path("/jobs/h3"))
    assert _matches(manager, {}, {"config": {"recipe": "h3md"}}, "workflow", "h3md", None)
    assert not _matches(manager, {}, {"config": {"recipe": "h3md"}}, "workflow", None, None)
    assert not _matches(manager, {}, {"config": {"recipe": "director"}}, "workflow", "h3md", None)


def test_partial_rerun_patch_keeps_shots_and_pass_strengths(inventory):
    from src.video_queue_bulk import _patched
    raw = draft(inventory)
    resolved = h3.validate_config(raw, inventory["components"], inventory["gpus"], {})
    updated = _patched(resolved, {"h3md": {"resume_from_cache": True, "rerun_segments": "2"}}, inventory, "h3", {})
    assert updated["h3md"]["shots"] == raw["h3md"]["shots"]
    assert updated["h3md"]["high_res_strengths"] == [1, 1]
    assert updated["h3md"]["resume_from_cache"] and updated["h3md"]["rerun_segments"] == "2"


def test_rerun_detaches_shared_segment_before_overwrite(tmp_path):
    from scripts.h3md_worker import detach_segment_files
    source, target = tmp_path / "source", tmp_path / "target"
    cache = source / "h3md-cache/resume_1"
    cache.mkdir(parents=True)
    original = cache / "seg_001_frames.npy"
    original.write_bytes(b"original frames")
    h3md.clone_cache(source, target)
    copied = target / "h3md-cache/resume_1"
    detach_segment_files(SimpleNamespace(_p=lambda index, suffix: copied / f"seg_{index:03}{suffix}"), 1)
    (copied / original.name).write_bytes(b"new frames")
    assert original.read_bytes() == b"original frames"


def test_portable_workflow_keeps_shots_and_both_pass_strengths(inventory):
    from src.video_workflow import resolve_draft, validate_document, component_reference, FORMAT
    raw = draft(inventory)
    raw["h3md"]["high_res_strengths"] = [.25, .75]
    raw["h3md"]["shots"][0]["prompt"] = "The second-pass strengths must survive export."
    config, selected = resolve_draft(raw, inventory, "h3")
    document = {"format": FORMAT, "version": 1, "family": "h3", "config": config,
                "components": {key: component_reference(path) for key, path in selected.items()}, "attachments": []}
    validate_document(document, "h3")
    assert config["h3md"] == raw["h3md"]
    assert config["lora_strengths"] == [1, 1]
    assert Path(selected["latent_upscaler"]).name == controls.UPSCALER
