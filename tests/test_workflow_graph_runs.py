"""Graph execution uses durable artifacts and the existing owned video jobs."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
import zipfile

import pytest

from src import h3_video as h3
from src.workflow_graph_runs import GraphRunManager
from tests.test_video_submission import backend, image
from tests.test_h3_seed_hunter_api import hunter, final_body
from tests.test_h3_video import weights


def wait_for(manager, run_id, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.view(run_id, "corey")
        if predicate(result):
            return result
        time.sleep(.03)
    pytest.fail("Workflow did not reach expected state: " + json.dumps(result))


def step(result, identity):
    return next(row for row in result["steps"] if row["id"] == identity)


def native_plan(backend):
    return {"valid": True, "issues": [], "plan": [
        {"id": "render", "node_type": "render.h3", "family": "h3", "operation": "submit",
         "config": backend.configs["h3"], "inputs": {"reference_videos": ["clip"]}, "dependencies": []},
        {"id": "swap", "node_type": "render.bfs", "family": "bfs", "operation": "submit",
         "config": backend.configs["bfs"], "inputs": {"source_video": ["render"], "identity_image": ["identity"]},
         "dependencies": ["render"]}], "outputs": [{"id": "out", "title": "Result", "source": "swap"}]}


def staged(tmp_path):
    video, identity = tmp_path / "source.mp4", tmp_path / "head.png"
    video.write_bytes(b"synthetic source")
    identity.write_bytes(image())
    return {"clip": [{"path": str(video), "name": "source.mp4"}],
            "identity": [{"path": str(identity), "name": "head.png"}]}


def complete(manager, job_id, data=b"synthetic output"):
    directory = manager.directory(job_id)
    (directory / "output.mp4").write_bytes(data)
    h3._write(directory / "state.json", {**h3._read(directory / "state.json"), "status": "completed"})


def test_native_dependencies_restart_retries_and_ownership(backend, tmp_path):
    root = tmp_path / "runs"
    manager = GraphRunManager(root, backend.managers, poll_interval=.02)
    request_id = str(uuid.uuid4())
    try:
        media = staged(tmp_path)
        run = manager.accept("corey", {"title": "Two native stages"}, native_plan(backend), media, request_id)
        run = wait_for(manager, run["id"], lambda value: step(value, "render").get("submission_confirmed"))
        assert len(backend.calls) == 1
        assert step(run, "swap")["status"] == "pending"
        # Retry returns the immutable accepted run even after uploads disappear.
        Path(media["clip"][0]["path"]).unlink()
        assert manager.accept("corey", {}, {}, {}, request_id)["id"] == run["id"]
        assert manager.existing("corey", request_id)["id"] == run["id"]
        assert manager.existing("other", request_id) is None
        assert manager.list("other") == []
        with pytest.raises(FileNotFoundError):
            manager.view(run["id"], "other")
        with pytest.raises(FileNotFoundError):
            manager.cancel(run["id"], "other")
        manager.close()
        # Simulate a crash after native acceptance and before graph publication.
        path = root / run["id"] / "state.json"
        state = h3._read(path)
        state["steps"]["render"]["submission_confirmed"] = False
        h3._write(path, state)
        manager = GraphRunManager(root, backend.managers, poll_interval=.02)
        assert manager.recover() == 1
        run = wait_for(manager, run["id"], lambda value: step(value, "render").get("submission_confirmed"))
        assert len(backend.calls) == 1
        complete(backend.managers["h3"], step(run, "render")["job_id"], b"original upstream result")
        run = wait_for(manager, run["id"], lambda value: step(value, "swap").get("submission_confirmed"))
        assert len(backend.calls) == 2
        swap = step(run, "swap")["job_id"]
        saved = h3._read(backend.managers["bfs"].directory(swap) / "manifest.json")
        assert Path(saved["source_video"]).read_bytes() == b"original upstream result"
        complete(backend.managers["bfs"], swap)
        run = wait_for(manager, run["id"], lambda value: value["status"] == "completed")
        artifact, name = manager.output_path(run["id"], "out", "corey")
        assert artifact.read_bytes() == b"synthetic output" and name.endswith(".mp4")
        assert run["outputs"][0]["available"]
        assert str(root) not in json.dumps(run)
        with pytest.raises(FileNotFoundError):
            manager.output_path(run["id"], "out", "other")
    finally:
        manager.close()


def test_all_bindings_and_native_configs_validate_before_any_launch(backend, tmp_path):
    manager = GraphRunManager(tmp_path / "runs", backend.managers)
    try:
        with pytest.raises(ValueError, match="Attach media"):
            manager.accept("corey", {}, native_plan(backend), {}, str(uuid.uuid4()))
        plan = native_plan(backend)
        plan["plan"][1]["config"] = {**plan["plan"][1]["config"], "gpu": "not-installed"}
        with pytest.raises(ValueError, match="GPU"):
            manager.accept("corey", {}, plan, staged(tmp_path), str(uuid.uuid4()))
        assert not backend.calls
        assert manager.list("corey") == []
    finally:
        manager.close()


def test_installed_comfy_execution_preserves_file_bindings_and_zip_result(backend, tmp_path):
    from src.comfy_node_manifest import source_receipt, normalize_node_info
    from src.comfy_workflow import ComfyJobManager
    package=tmp_path/'installed-package';package.mkdir()
    (package/'__init__.py').write_text('NODE_CLASS_MAPPINGS = {}\n')
    receipt=source_receipt(package)
    schema=normalize_node_info('FixtureSaveImage',{'input':{'required':{'image':[['placeholder.png'],{'image_upload':True}]}},'output':[],'output_node':True})
    manifest={'status':'ready','package_id':receipt['id'],'source':receipt,'package':receipt,'nodes':[schema]}
    manifests=tmp_path/'manifests';manifests.mkdir()
    (manifests/'fixture.json').write_text(json.dumps(manifest))
    native=ComfyJobManager(tmp_path/'jobs'/'comfy',project=tmp_path,manifests=manifests)
    native.inventory=lambda:{'runtime_ready':True,'gpus':[],'components':[]}
    manager=GraphRunManager(tmp_path/'runs',{'comfy':native},poll_interval=.02)
    config={'prompt':{'sink':{'class_type':'FixtureSaveImage','inputs':{}}},'output_nodes':['sink'],
            'packages':[{'id':receipt['id'],'fingerprint':receipt['fingerprint']}], 'gpu':'cpu',
            'external_inputs':{'sink':{'image':{'kind':'FILE','media_kind':'image','binding':'asset_0'}}}}
    plan={'valid':True,'plan':[{'id':'execute','family':'comfy','operation':'execute','config':config,
                              'inputs':{'asset_0':['sink::image']},'dependencies':[]}],
          'outputs':[{'id':'archive','source':'execute'}]}
    input_file=tmp_path/'image.png';input_file.write_bytes(image())
    document={'title':'Installed package graph','nodes':[{'id':'sink'}]}
    try:
        run=manager.accept('corey',document,plan,{'sink::image':[{'path':str(input_file),'name':'image.png'}]},str(uuid.uuid4()))
        run=wait_for(manager,run['id'],lambda r: step(r,'execute').get('submission_confirmed'))
        assert len(backend.calls)==1
        directory=native.directory(step(run,'execute')['job_id'])
        saved=h3._read(directory/'manifest.json')
        assert saved['config']['gpu']=='cpu' and saved['config']['prompt']==config['prompt']
        assert saved['config']['external_inputs']==config['external_inputs']
        assert Path(saved['asset_0'][0]).read_bytes()==image()
        assert Path(saved['asset_0'][0]).parent==directory
        assert manager.detail(run['id'],'corey')['document']==document
        with zipfile.ZipFile(directory/'output.zip','w') as archive:
            archive.writestr('result.json','{"status":"completed"}')
        h3._write(directory/'state.json',{**h3._read(directory/'state.json'),'status':'completed'})
        run=wait_for(manager,run['id'],lambda r: r['status']=='completed')
        path,name=manager.output_path(run['id'],'archive','corey')
        assert path.suffix=='.zip' and name.endswith('.zip')
        assert zipfile.is_zipfile(path) and run['outputs'][0]['available']
    finally:
        manager.close()


def test_failed_dependency_blocks_downstream_without_submitting_it(backend, tmp_path):
    manager = GraphRunManager(tmp_path / "runs", backend.managers, poll_interval=.02)
    try:
        run = manager.accept("corey", {}, native_plan(backend), staged(tmp_path), str(uuid.uuid4()))
        run = wait_for(manager, run["id"], lambda value: step(value, "render").get("submission_confirmed"))
        path = backend.managers["h3"].directory(step(run, "render")["job_id"]) / "state.json"
        h3._write(path, {**h3._read(path), "status": "failed", "error": "Synthetic upstream failure"})
        run = wait_for(manager, run["id"], lambda value: value["status"] == "failed")
        assert step(run, "render")["status"] == "failed"
        assert step(run, "swap")["status"] == "blocked"
        assert len(backend.calls) == 1
    finally:
        manager.close()


def test_cancel_is_run_scoped_and_never_submits_downstream(backend, tmp_path, monkeypatch):
    stopped = []
    def cancel(job_id, owner):
        stopped.append((job_id, owner))
        directory = backend.managers["h3"].directory(job_id)
        h3._write(directory / "state.json", {**h3._read(directory / "state.json"), "status": "stopped"})
    monkeypatch.setattr(backend.managers["h3"], "cancel", cancel)
    manager = GraphRunManager(tmp_path / "runs", backend.managers, poll_interval=.02)
    try:
        run = manager.accept("corey", {}, native_plan(backend), staged(tmp_path), str(uuid.uuid4()))
        run = wait_for(manager, run["id"], lambda value: step(value, "render").get("submission_confirmed"))
        original = step(run, "render")["job_id"]
        manager.cancel(run["id"], "corey")
        run = wait_for(manager, run["id"], lambda value: value["status"] == "cancelled")
        assert stopped == [(original, "corey")]
        assert step(run, "swap")["status"] == "cancelled"
        assert len(backend.calls) == 1
    finally:
        manager.close()


def test_concurrent_accept_deduplicates_run_and_native_submission(backend, tmp_path):
    manager = GraphRunManager(tmp_path / "runs", backend.managers, poll_interval=.02)
    try:
        media, key = staged(tmp_path), str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=3) as pool:
            replies = list(pool.map(lambda _: manager.accept("corey", {}, native_plan(backend), media, key), range(3)))
        assert len({result["id"] for result in replies}) == 1
        wait_for(manager, replies[0]["id"], lambda value: step(value, "render").get("submission_confirmed"))
        assert len(backend.calls) == 1
    finally:
        manager.close()


def test_refinement_clones_selected_candidate_with_native_idempotent_api(hunter, tmp_path):
    manager = GraphRunManager(tmp_path / "runs", hunter.managers, poll_interval=.02)
    plan = {"valid": True, "plan": [
        {"id": "preview", "node_type": "hunter.preview", "family": "h3", "operation": "submit",
         "config": hunter.configs["h3"], "inputs": {}, "dependencies": []},
        {"id": "refine", "node_type": "hunter.refine", "family": "h3", "operation": "refine",
         "config": final_body(hunter)["config"], "inputs": {}, "depends_on": "preview",
         "candidate": 2, "dependencies": ["preview"]}], "outputs": [{"id": "final", "source": "refine"}]}
    try:
        run = manager.accept("corey", {}, plan, {}, str(uuid.uuid4()))
        run = wait_for(manager, run["id"], lambda value: step(value, "preview").get("submission_confirmed"))
        source_id = step(run, "preview")["job_id"]
        source = hunter.managers["h3"].directory(source_id)
        config = h3._read(source / "manifest.json")["config"]
        for index in (1, 2, 3):
            (source / f"candidate-{index}.mp4").write_bytes(f"preview{index}".encode())
            h3._write(source / f"candidate-{index}.json", {"config": config, "chosen": index})
            weights(source / f"candidate-{index}.safetensors")
        h3._write(source / "candidates.json", {"candidates": [
            {"id": index, "seed": index * 1000, "status": "completed", "latent_ready": True} for index in (1, 2, 3)]})
        complete(hunter.managers["h3"], source_id)
        run = wait_for(manager, run["id"], lambda value: step(value, "refine").get("submission_confirmed"))
        final_id = step(run, "refine")["job_id"]
        final = hunter.managers["h3"].directory(final_id)
        saved = h3._read(final / "manifest.json")
        assert saved["seed_hunter_source"] == {
            "source_job_id": source_id, "source_candidate": 2, "source_seed": 2000,
            "preview_frames": 73, "regeneration_required": False,
        }
        assert h3._read(final / "source-latent.json")["chosen"] == 2
        assert saved["config"]["prompt"] == config["prompt"]
        assert saved["config"]["seed_hunter"]["stage"] == "final"
        assert len(hunter.calls) == 2
        complete(hunter.managers["h3"], final_id)
        wait_for(manager, run["id"], lambda value: value["status"] == "completed")
    finally:
        manager.close()


def test_second_coordinator_cannot_launch_or_cancel_unrecorded_duplicate(backend, tmp_path, monkeypatch):
    root = tmp_path / "runs"
    manager = GraphRunManager(root, backend.managers, poll_interval=.02)
    second = GraphRunManager(root, backend.managers, poll_interval=.02)
    stopped = []
    monkeypatch.setattr(backend.managers["h3"], "cancel", lambda identity, owner: stopped.append((identity, owner)))
    try:
        run = manager.accept("corey", {}, native_plan(backend), staged(tmp_path), str(uuid.uuid4()))
        run = wait_for(manager, run["id"], lambda value: step(value, "render").get("submission_confirmed"))
        second.recover()
        time.sleep(.1)
        assert len(backend.calls) == 1
        manager.close()
        # A restart sees the expected job ID even when confirmation was lost.
        path = root / run["id"] / "state.json"
        state = h3._read(path)
        state["steps"]["render"]["submission_confirmed"] = False
        h3._write(path, state)
        second.cancel(run["id"], "corey")
        result = wait_for(second, run["id"], lambda value: value["status"] == "cancelled")
        assert stopped == [(step(run, "render")["job_id"], "corey")]
        assert step(result, "swap")["status"] == "cancelled" and len(backend.calls) == 1
    finally:
        manager.close()
        second.close()


def test_real_cpu_trim_resize_save_end_to_end(tmp_path):
    from scripts.h3_seed_hunter import ffmpeg_binary
    import av
    source = tmp_path / "test-source.mp4"
    subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=160x120:rate=24:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    plan = {"valid": True, "plan": [
        {"id": "trim", "family": "media", "operation": "trim", "params": {"start": .5, "end": 1.5},
         "inputs": {"video": ["source"]}, "dependencies": []},
        {"id": "resize", "family": "media", "operation": "resize", "params": {"width": 96, "height": 64, "fit": "cover"},
         "inputs": {"video": ["trim"]}, "dependencies": ["trim"]},
        {"id": "save", "family": "media", "operation": "save", "params": {"name": "result.mp4"},
         "inputs": {"video": ["resize"]}, "dependencies": ["resize"]}],
        "outputs": [{"id": "save", "title": "Processed video", "source": "save"}]}
    manager = GraphRunManager(tmp_path / "runs", {}, poll_interval=.02)
    try:
        run = manager.accept("corey", {"title": "CPU graph"}, plan,
                             {"source": [{"path": str(source), "name": source.name}]}, str(uuid.uuid4()))
        run = wait_for(manager, run["id"], lambda value: value["status"] in {"completed", "failed"})
        assert run["status"] == "completed", run
        output, name = manager.output_path(run["id"], "save", "corey")
        assert name == "result.mp4"
        with av.open(str(output)) as video:
            stream = video.streams.video[0]
            frames = list(video.decode(video=0))
            assert (stream.width, stream.height, len(frames)) == (96, 64, 24)
        source.unlink()
        assert output.is_file()
    finally:
        manager.close()


def test_cpu_recovery_stops_only_its_orphan_before_reprocessing(tmp_path):
    from scripts.h3_seed_hunter import ffmpeg_binary
    source = tmp_path / "source.mp4"
    subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=size=64x64:rate=24:duration=0.5", "-c:v", "libx264", str(source)], check=True)
    manager = GraphRunManager(tmp_path / "runs", {}, poll_interval=.02)
    # Leave a durable accepted run with no current coordinator, as after a crash.
    manager._shutdown.set()
    plan = {"valid": True, "plan": [{"id": "save", "family": "media", "operation": "save",
             "inputs": {"video": ["source"]}, "dependencies": [], "params": {}}],
            "outputs": [{"id": "out", "source": "save"}]}
    run = manager.accept("corey", {}, plan, {"source": [{"path": str(source), "name": source.name}]}, str(uuid.uuid4()))
    directory = manager.root / run["id"]
    process = subprocess.Popen([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
                                "color=size=64x64:rate=24:duration=30", "-c:v", "libx264", str(directory / "orphan.mp4")],
                               env={**os.environ, "ODYSSEUS_WORKFLOW_CPU_RUN": run["id"]},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reaper = threading.Thread(target=process.wait, daemon=True)
    reaper.start()
    replacement = GraphRunManager(manager.root, {}, poll_interval=.02)
    try:
        assert replacement.recover() == 1
        result = wait_for(replacement, run["id"], lambda value: value["status"] in {"completed", "failed"})
        assert result["status"] == "completed", result
        reaper.join(3)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        reaper.join(3)
        replacement.close()
