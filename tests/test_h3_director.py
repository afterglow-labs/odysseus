"""Director stays an independent recipe across validation, queue and artifacts."""
import ast
from copy import deepcopy
import json
import math
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import h3_director_controls as controls
from scripts.h3_director_worker import timeline
from src import h3_director as director, h3_video as h3
from src.video_job_listing import list_options
from src.video_history_delete import _matches
from src.video_queue_bulk import _patched
from tests.test_h3_video import inventory, config, api, weights
from tests.test_video_submission import backend, send


def draft(inv, **options):
    return {**config(inv, "ref2va"), **director.capabilities(inv["components"])["defaults"],
            "director": {**deepcopy(controls.DEFAULTS), **options}}


@pytest.mark.parametrize("mode", ["ref2va", "fl2va"])
def test_director_is_independent_and_allows_zero_reference_generation(inventory, mode):
    raw = draft(inventory, duration_seconds=60, candidate_3=True)
    raw.update(mode=mode, model=config(inventory, mode)["model"])
    resolved = h3.validate_config(raw, inventory["components"], inventory["gpus"], {})
    assert resolved["recipe"] == "director" and "seed_hunter" not in resolved
    assert resolved["director"]["duration_seconds"] == 60
    assert controls.candidate_indices(resolved["director"]) == [1, 3]
    assert resolved["steps"] == 20 and resolved["shift_audio"] == 3


@pytest.mark.parametrize("options", [dict(candidate_2=1), dict(stage=[]), dict(resize_method={}),
    dict(duration_seconds=float("nan")), dict(duration_seconds=121), dict(chunk_duration_seconds=16),
    dict(characters=[dict(image_index=0, file="/etc/passwd")]), dict(refVideos=[dict(input_index=0, trimStartSec=4, trimEndSec=3)]),
    dict(segments=[]), dict(segments=[dict(id="a",weight=0)]), dict(segments=[dict(id="a"),dict(id="a")])])
def test_director_rejects_untrusted_paths_invalid_timing_and_switches(options):
    with pytest.raises(ValueError): controls.validate_options(options)


def test_timeline_maps_owned_inputs_preserves_slot_ids_and_location():
    raw = {"mode":"ref2va", "prompt":"Soft daylight", "director": controls.validate_options({
        "characters":[dict(image_index=2, description="Blue coat")], "background":dict(image_index=1),
        "segments":[dict(id="cut",prompt='Says "hello".',speakerCharIdxs=[2])],
        "refVideos":[dict(input_index=0,trimStartSec=2,trimEndSec=7,includeAudio=True,role="editing_source")]})}
    media = dict(reference_images=["/owned/a.png","/owned/b.png","/owned/c.png"], reference_videos=["/owned/v.mp4"],reference_audio=[])
    data=timeline(raw, media)
    assert data["characters"][1] == {}
    assert data["characters"][2]["description"] == "Blue coat"
    assert data["background"]["file"] == "/owned/b.png"
    assert data["refVideos"][0]["trimStartSec"] == 2
    assert data["segments"][0]["speakerCharIdxs"] == [2]
    assert "image_index" not in data["characters"][2]


def test_actual_upstream_chunk_bucketing_keeps_overlapping_cuts_and_15s_ceiling():
    # Run the vendored pure function without importing the GPU inference library.
    path=Path("scripts/vendor/muse_minimax_director.py")
    source=ast.parse(path.read_text())
    function=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=="_bucket_segments_into_chunks")
    namespace={"math":math};exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),"exec"),namespace)
    bucket=namespace[function.name]
    for duration,chunk in [(15,10),(16,15),(31,15),(120,15),(25,10)]:
        buckets,lengths,bounds=bucket({"segments":[dict(prompt="Keep walking",weight=1)]},duration,chunk)
        assert all(group[0]["prompt"]=="Keep walking" for group in buckets)
        assert all(length<=15 for length in lengths) and sum(lengths)==duration
        assert lengths==controls.chunk_lengths(dict(duration_seconds=duration,chunk_duration_seconds=chunk))


def test_hybrid_requires_a_real_fl_checkpoint_and_allows_no_legacy_fallback(inventory):
    raw=draft(inventory,hybrid_continuation=True)
    raw.pop("model_fl2va")
    with pytest.raises(ValueError,match="checkpoint"):h3.validate_config(raw,inventory["components"],inventory["gpus"],{})
    raw["model_fl2va"]={}
    with pytest.raises(ValueError):h3.validate_config(raw,inventory["components"],inventory["gpus"],{})
    raw["model_fl2va"]=raw["model"]
    with pytest.raises(ValueError,match="checkpoint"):h3.validate_config(raw,inventory["components"],inventory["gpus"],{})


def test_director_history_and_deletion_scopes_do_not_overlap_hunter():
    assert list_options({"recipe":"director"},"h3")["recipe"]=="director"
    manager=SimpleNamespace(root=Path("/jobs/h3"));manifest={"config":{"recipe":"director"}}
    assert _matches(manager,{},manifest,"workflow","director",None)
    assert not _matches(manager,{},manifest,"workflow","seed_hunter","not_scout")
    assert not _matches(manager,{},manifest,"workflow",None,None)


def test_director_sparse_rerun_patch_retains_timeline_and_candidate_identity(inventory):
    original=h3.validate_config(draft(inventory,candidate_3=True,duration_seconds=25),inventory["components"],inventory["gpus"],{})
    updated=_patched(original,{"director":{"overall_soundscape":"River water"}},inventory,"h3",{})
    assert updated["director"]["duration_seconds"]==25 and updated["director"]["candidate_3"]
    assert updated["director"]["overall_soundscape"]=="River water"
    with pytest.raises(ValueError,match="stage"):
        _patched(original,{"director":{"stage":"refine"}},inventory,"h3",{})


def test_snapshot_clone_survives_parent_deletion_and_rejects_external_paths(tmp_path):
    source=tmp_path/"parent";source.mkdir();destination=tmp_path/"child";destination.mkdir()
    spec={}
    for key,suffix in [("tensors",".safetensors"),("metadata",".json")]:
        path=source/("candidate-3"+suffix);path.write_bytes(b"fixture");spec[key]=str(path)
    copied=director.clone_snapshot(source,destination,spec)
    Path(spec["tensors"]).unlink()
    assert Path(copied["tensors"]).read_bytes()==b"fixture"
    with pytest.raises(ValueError,match="outside"):
        director.clone_snapshot(source,destination,copied)


def test_api_accepts_independent_recipe_but_cannot_forge_refinement_snapshot(api,monkeypatch):
    client,manager,inv=api;captured=[]
    monkeypatch.setattr(manager,"launch",lambda directory,owner,cfg,uploads: captured.append(cfg) or dict(id=directory.name,status="queued"))
    response=client.post("/api/video/h3/jobs",data={"config":json.dumps(draft(inv,candidate_4=True))})
    assert response.status_code==201,response.text
    assert controls.candidate_indices(captured[0]["director"])==[1,4]
    response=client.post("/api/video/h3/jobs",data={"config":json.dumps(draft(inv,stage="refine"))})
    assert response.status_code==400 and "saved Director" in response.text
    response=client.get("/api/video/h3/jobs/"+"a"*32+"/director/candidates/1/video",headers={"x-user":"other"})
    assert response.status_code==404


def test_pinkcherry_pair_is_specific_to_the_published_model(tmp_path,inventory):
    from scripts.h3_seed_hunter_presets import PINKCHERRY,CHARACTER_TURBO
    from src.h3_seed_hunter_presets import catalog
    weights(tmp_path/PINKCHERRY);weights(tmp_path/CHARACTER_TURBO,lora=True)
    inventory["components"]=h3.discover_components([tmp_path])
    rows={r["id"]:r for r in catalog(inventory["components"])}
    assert "director" not in rows
    for key,steps in [("pinkcherry12",12),("pinkcherry4",4)]:
        raw={**config(inventory,"ref2va"),**rows[key]["config"],"recipe":"seed_hunter","frames":73}
        resolved=h3.validate_config(raw,inventory["components"],inventory["gpus"],{})
        assert resolved["steps"]==steps and resolved["sampler"]=="euler"
        assert resolved["scheduler"]=="simple" and not resolved["seed_hunter"]["shift_enabled"]


def test_refinement_is_owned_idempotent_and_keeps_its_snapshot_after_parent_deletion(backend):
    backend.configs["h3"] = draft(backend.inventories["h3"], candidate_3=True)
    backend.configs["h3"]["gpu"] = backend.inventories["h3"]["gpus"][0]["id"]
    response = send(backend, "h3")
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    manager = backend.managers["h3"]
    parent = manager.directory(job_id)
    state = h3._read(parent / "state.json")
    h3._write(parent / "state.json", {**state, "status":"completed"})
    for suffix in ("mp4", "safetensors", "json"):
        (parent / ("candidate-3." + suffix)).write_bytes(b"saved candidate")
    h3._write(parent / "director-candidates.json", {"candidates":[dict(index=3, seed=2_000_048, can_refine=True)]})
    endpoint = f"/api/video/h3/jobs/{job_id}/director/refine"
    body = dict(request_id=str(uuid.uuid4()), candidate=3, config=dict(steps=9, denoise=.3))
    client = backend.client()
    assert client.post(endpoint, json=body, headers={"x-user":"other"}).status_code == 404
    assert client.post(endpoint, json=body, headers={"x-user":"reader"}).status_code == 403
    response = client.post(endpoint, json=body)
    assert response.status_code == 201, response.text
    child_id = response.json()["id"]
    manifest = h3._read(manager.directory(child_id) / "manifest.json")
    assert manifest["config"]["director"]["stage"] == "refine"
    assert manifest["config"]["seed"] == 2_000_048 and manifest["config"]["steps"] == 9
    assert manifest["config"]["director"]["denoise"] == .3
    assert manifest["director_source"]["candidate"] == 3
    original_snapshot = Path(manifest["director_input"]["tensors"])
    assert original_snapshot.parent == manager.directory(child_id)
    draft_response = client.get(f"/api/video/h3/jobs/{child_id}/edit").json()
    rejected = client.patch(f"/api/video/h3/jobs/{child_id}", data={"config":json.dumps(draft_response["config"]),
                           "revision":"0", "retain_inputs":"{}"})
    assert rejected.status_code == 400 and "original inputs" in rejected.text
    shutil.rmtree(parent)
    retried = client.post(endpoint, json=body)
    assert retried.status_code == 201 and retried.json()["id"] == child_id
    assert original_snapshot.read_bytes() == b"saved candidate"
    assert len(backend.calls) == 2


def test_base_rerun_retains_media_direction_but_uses_new_timeline(inventory):
    from src.video_queue_bulk import _from_base
    original = draft(inventory, refVideos=[dict(input_index=0, trimStartSec=4, trimEndSec=7, description="Original river")])
    original = h3.validate_config(original, inventory["components"], inventory["gpus"], {"reference_videos":["owned.mp4"]})
    base = draft(inventory, duration_seconds=25, refVideos=[dict(input_index=2, trimStartSec=40)])
    updated = _from_base(original, base, inventory, "h3", {"reference_videos":["owned.mp4"]})
    assert updated["director"]["duration_seconds"] == 25
    assert updated["director"]["refVideos"] == original["director"]["refVideos"]
