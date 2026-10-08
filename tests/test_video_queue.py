"""Exercise detached, mixed-workflow FIFO execution without loading any models."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import time

import pytest

from src import h3_video as h3
from src.bfs_video import BFSJobManager


def wait_for(read, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(.05)
    raise AssertionError(f"Timed out: {value}")


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "gallery.db"))
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "data"))
    worker = tmp_path / "fake_video_worker.py"
    worker.write_text('''import fcntl,json,os,sys,time
from pathlib import Path
job=json.loads(Path(sys.argv[2]).read_text()); cfg=job['config']
directory=Path(sys.argv[2]).parent; root=Path(cfg['test_root'])
try:
    lease=os.open(root/'exclusive', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
except FileExistsError:
    (root/'overlap').touch(); raise
try:
    with (root/'events.jsonl').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps({'id':directory.name,'config':cfg,
            'image':Path(job['first_frame']).read_text() if job.get('first_frame') else None})+'\\n')
    (directory/'started').touch()
    deadline=time.monotonic()+20
    while cfg.get('hold') and not (directory/'release').exists():
        if time.monotonic()>deadline: raise RuntimeError('Test release timed out')
        time.sleep(.05)
    time.sleep(.05)
    if cfg.get('fail'): raise RuntimeError('Deliberate failed render')
    Path(job['output_path']).write_bytes(b'fixture-MP4')
finally:
    os.close(lease); (root/'exclusive').unlink(missing_ok=True)
''')
    managers = [h3.H3JobManager(tmp_path / "jobs" / "h3", worker=worker, gallery_directory=tmp_path / "gallery"),
                BFSJobManager(tmp_path / "jobs" / "bfs", worker=worker, gallery_directory=tmp_path / "gallery")]
    jobs = []

    def launch(index=0, **settings):
        manager = managers[index]
        directory = manager.stage()
        cfg = {"prompt": "Original prompt", "model": "fixture-model.safetensors", "steps": 2,
               "gpu": "GPU-fixture", "width": 256, "height": 256, "seed": 42,
               "test_root": str(tmp_path), **settings}
        if index:
            cfg.update(workflow_id="h3_head_swap", workflow_label="H3 head swap", family="h3")
        image = directory / "input.png"
        image.write_text(f"image for {directory.name}")
        job = manager.launch(directory, "corey", cfg, {"first_frame": str(image)})
        jobs.append((manager, directory))
        return directory, cfg, job

    yield managers, launch, tmp_path
    for manager, directory in jobs:
        if directory.exists():
            manager.cancel(directory.name, "corey")


def test_mixed_fifo_cancel_and_failure_advance_without_browser_or_manager_poll(queue):
    managers, launch, root = queue
    first, _, _ = launch(hold=True)
    wait_for(lambda: (first / "started").exists(), bool)
    cancelled, _, cancelled_view = launch(1)
    failed, _, failed_view = launch(fail=True)
    last, last_config, last_view = launch(1, prompt="The last saved prompt", seed=123)
    assert [job["queue_position"] for job in (cancelled_view, failed_view, last_view)] == [1, 2, 3]
    assert all(job["started_at"] is None for job in (cancelled_view, failed_view, last_view))
    # Editing the caller's next draft cannot alter an already accepted snapshot.
    last_config.update(prompt="A later draft", seed=999)
    assert managers[1].cancel(cancelled.name, "corey")["status"] == "stopped"
    assert managers[0].view(failed.name, "corey")["queue_position"] == 1
    (first / "release").touch()
    # Read only persisted receipts: no manager/API call drives dispatch.
    wait_for(lambda: h3._read(last / "state.json"), lambda row: row.get("status") not in h3.ACTIVE)
    states = [h3._read(directory / "state.json") for directory in (first, cancelled, failed, last)]
    assert [row["status"] for row in states] == ["completed", "stopped", "failed", "completed"]
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert [event["id"] for event in events] == [first.name, failed.name, last.name]
    assert events[-1]["config"]["prompt"] == "The last saved prompt"
    assert events[-1]["config"]["seed"] == 123
    assert events[-1]["image"] == f"image for {last.name}"
    assert states[0]["finished_at"] <= states[2]["started_at"]
    assert states[2]["finished_at"] <= states[3]["started_at"]
    assert not (cancelled / "started").exists() and not (root / "overlap").exists()
    restarted = BFSJobManager(managers[1].root)
    assert restarted.view(last.name, "corey")["queue_position"] is None
    assert restarted.view(last.name, "corey")["started_at"] == states[-1]["started_at"]


def test_concurrent_h3_bfs_submission_order_matches_execution_order(queue):
    _, launch, root = queue
    first, _, _ = launch(hold=True)
    wait_for(lambda: (first / "started").exists(), bool)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda index: launch(index % 2, seed=index), range(4)))
    ordered = sorted((directory for directory, _, _ in jobs),
                     key=lambda directory: h3._read(directory / "state.json")["queue_order"])
    assert len({h3._read(directory / "state.json")["queue_order"] for directory in ordered}) == 4
    (first / "release").touch()
    wait_for(lambda: [h3._read(path / "state.json")["status"] for path in ordered],
             lambda statuses: all(status not in h3.ACTIVE for status in statuses))
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert [event["id"] for event in events] == [first.name] + [path.name for path in ordered]
    assert all(h3._read(path / "state.json")["status"] == "completed" for path in ordered)
    assert not (root / "overlap").exists()


def test_waiting_job_advances_after_legacy_render_completes_without_new_supervisor(queue):
    managers, launch, _ = queue
    legacy = managers[0].stage()
    state = {"id": legacy.name, "owner": "corey", "status": "running", "created_at": time.time(),
             "supervisor": h3._identity(os.getpid())}
    h3._write(legacy / "state.json", state)
    waiting, _, view = launch(1)
    assert view["status"] == "queued" and view["queue_position"] == 1
    time.sleep(.7)
    assert not (waiting / "started").exists()
    # An older detached supervisor publishes completion but knows nothing about
    # the new dispatcher. The waiting supervisor must notice on its own.
    h3._write(legacy / "state.json", {**state, "status": "completed"})
    result = wait_for(lambda: h3._read(waiting / "state.json"), lambda row: row.get("status") not in h3.ACTIVE)
    assert result["status"] == "completed"


def test_orphaned_running_worker_keeps_queue_blocked_until_confirmed_cancel(queue):
    managers, launch, root = queue
    running, _, _ = launch(hold=True)
    wait_for(lambda: (running / "started").exists(), bool)
    waiting, _, _ = launch(1)
    state = h3._read(running / "state.json")
    assert h3._live_record(state["supervisor"])
    os.kill(state["supervisor"]["pid"], signal.SIGKILL)
    wait_for(lambda: h3._live_record(state["supervisor"]), lambda live: not live)
    assert h3._live_record(state["worker"])
    time.sleep(.7)
    assert not (waiting / "started").exists()
    assert managers[0].view(running.name, "corey")["status"] == "running"
    assert managers[0].cancel(running.name, "corey")["status"] == "stopped"
    result = wait_for(lambda: h3._read(waiting / "state.json"), lambda row: row.get("status") not in h3.ACTIVE)
    assert result["status"] == "completed"
    assert not h3._live_record(state["worker"])
    assert not (root / "overlap").exists()


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_200_job_list_scans_queue_and_processes_once(tmp_path, monkeypatch, family):
    manager_type = h3.H3JobManager if family == 'h3' else BFSJobManager
    manager = manager_type(tmp_path / family)
    identifiers = []
    for index in range(200):
        directory = manager.stage()
        identifiers.append(directory.name)
        h3._write(directory / 'state.json', {'id': directory.name, 'owner': 'corey', 'status': 'queued',
            'queue_order': index, 'created_at': time.time(), 'supervisor': {'pid': 1234, 'start': 'alive'}})
        h3._write(directory / 'manifest.json', {'config': {'prompt': str(index)}})
    calls = {'queue': 0, 'processes': 0}
    original_queue = h3._queue_entries
    def queue_entries(*args, **kwargs):
        calls['queue'] += 1
        return original_queue(*args, **kwargs)
    def snapshot():
        calls['processes'] += 1
        return {1234: (1, 'alive')}
    monkeypatch.setattr(h3, '_queue_entries', queue_entries)
    monkeypatch.setattr(h3, '_snapshot', snapshot)
    rows = manager.jobs('corey')
    assert len(rows) == 200 and calls == {'queue': 1, 'processes': 1}
    positions = {row['id']: row['queue_position'] for row in rows}
    assert [positions[key] for key in identifiers] == list(range(1, 201))


def test_queue_reconciliation_shares_process_snapshot_across_families(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(h3, '_snapshot', lambda: calls.append(True) or {1234: (1, 'alive')})
    for family in ['h3', 'bfs']:
        manager = h3.H3JobManager(tmp_path / family)
        for index in range(100):
            directory = manager.stage()
            h3._write(directory / 'state.json', {'id': directory.name, 'owner': 'corey', 'status': 'queued',
                'queue_order': index, 'created_at': time.time(), 'supervisor': {'pid': 1234, 'start': 'alive'}})
    assert len(h3._queue_entries(tmp_path / 'h3', reconcile=True)) == 200
    assert calls == [True]


def test_pause_is_persistent_shared_and_allows_current_render_to_finish(queue):
    from src.video_queue_control import queue_status, set_queue_paused
    managers, launch, _ = queue
    running, _, _ = launch(hold=True)
    wait_for(lambda: (running / "started").exists(), bool)
    first, _, _ = launch(1)
    second, _, _ = launch()
    status = set_queue_paused(managers[0], "corey", True)
    assert status["paused"] and status["queued"] == 1 and status["total_queued"] == 2
    assert status["running"] == 1 and status["total_running"] == 1
    (running / "release").touch()
    wait_for(lambda: h3._read(running / "state.json"), lambda row: row.get("status") == "completed")
    time.sleep(.8)
    assert not (first / "started").exists() and not (second / "started").exists()
    restarted = BFSJobManager(managers[1].root)
    assert queue_status(restarted, "corey")["paused"]
    set_queue_paused(restarted, "corey", False)
    wait_for(lambda: h3._read(second / "state.json"), lambda row: row.get("status") == "completed")
    assert h3._read(first / "state.json")["finished_at"] <= h3._read(second / "state.json")["started_at"]


def test_pause_before_submission_and_other_owner_can_run(queue):
    from src.video_queue_control import set_queue_paused, queue_status
    managers, launch, root = queue
    set_queue_paused(managers[0], "corey", True)
    held, _, _ = launch()
    other = managers[1].stage()
    cfg = {"prompt": "Other owner", "model": "fixture", "steps": 2, "gpu": "0",
           "width": 256, "height": 256, "seed": 1, "test_root": str(root)}
    managers[1].launch(other, "another", cfg, {})
    try:
        wait_for(lambda: h3._read(other / "state.json"), lambda row: row.get("status") == "completed")
        assert not (held / "started").exists()
        assert not queue_status(managers[1], "another")["paused"]
    finally:
        managers[1].cancel(other.name, "another")


def test_cancel_all_queued_never_stops_running_and_keeps_inputs(queue):
    from src.video_queue_control import cancel_queued
    managers, launch, _ = queue
    running, _, _ = launch(hold=True)
    wait_for(lambda: (running / "started").exists(), bool)
    first, _, _ = launch(1)
    second, _, _ = launch()
    receipt = cancel_queued(managers[0], "corey", scope="all")
    assert receipt["succeeded"] == 2 and receipt["errors"] == []
    assert set(receipt["successes"]) == {first.name, second.name}
    assert h3._read(running / "state.json")["status"] == "running"
    assert (first / "input.png").exists() and (second / "manifest.json").exists()
    assert not h3._live_record(h3._read(first / "state.json")["supervisor"])


def test_delete_all_queued_removes_only_current_family(queue, monkeypatch):
    from src.video_queue_control import cancel_queued, set_queue_paused
    managers, launch, _ = queue
    monkeypatch.setattr(h3.H3JobManager, "_delete_gallery", lambda *args: None)
    set_queue_paused(managers[0], "corey", True)
    first, _, _ = launch()
    second, _, _ = launch()
    other, _, _ = launch(1)
    receipt = cancel_queued(managers[0], "corey", delete=True)
    assert receipt["succeeded"] == 2 and receipt["errors"] == []
    assert all(row["deleted"] for row in receipt["results"])
    assert not first.exists() and not second.exists()
    assert h3._read(other / "state.json")["status"] == "queued"


def test_bulk_snapshot_rejects_started_or_revised_job_and_ignores_later_submission(queue):
    from src.video_queue_control import cancel_queued, queued_targets
    managers, launch, _ = queue
    running, _, _ = launch(hold=True)
    wait_for(lambda: (running / "started").exists(), bool)
    first, _, _ = launch()
    revised, _, _ = launch()
    targets = queued_targets(managers[0], "corey")
    manifest = h3._read(revised / "manifest.json")
    h3._write(revised / "manifest.json", {**manifest, "revision": 1})
    later, _, _ = launch()
    targets.append({"id": running.name, "family": "h3", "revision": 0})
    receipt = cancel_queued(managers[0], "corey", targets)
    assert receipt["successes"] == [first.name] and receipt["failed"] == 2
    assert h3._read(running / "state.json")["status"] == "running"
    assert h3._read(revised / "state.json")["status"] == "queued"
    assert h3._read(later / "state.json")["status"] == "queued"


def test_migration_upgrades_only_waiters_preserving_receipts_and_pause(queue):
    from src.video_queue_control import upgrade_waiting_supervisors, set_queue_paused, queue_status
    managers, launch, _ = queue
    running, _, _ = launch(hold=True)
    wait_for(lambda: (running / "started").exists(), bool)
    waiting, _, _ = launch(1)
    before = h3._read(waiting / "state.json")
    manifest = (waiting / "manifest.json").read_bytes()
    current_running = h3._read(running / "state.json")
    h3._write(waiting / "state.json", {key: value for key, value in before.items()
                                      if key != "supervisor_control_version"})
    receipt = upgrade_waiting_supervisors(managers, "corey")
    assert receipt["upgraded"] == 1 and receipt["errors"] == []
    after = h3._read(waiting / "state.json")
    assert after["supervisor"] != before["supervisor"]
    assert not h3._live_record(before["supervisor"])
    assert after["supervisor_control_version"] == h3.SUPERVISOR_CONTROL_VERSION
    assert (waiting / "manifest.json").read_bytes() == manifest
    for key in ("owner", "queue_order", "created_at", "status"):
        assert after[key] == before[key]
    assert h3._read(running / "state.json")["worker"] == current_running["worker"]
    assert not queue_status(managers[0], "corey")["paused"]
    set_queue_paused(managers[0], "corey", True)
    (running / "release").touch()
    wait_for(lambda: h3._read(running / "state.json"), lambda row: row.get("status") == "completed")
    time.sleep(.7)
    assert not (waiting / "started").exists()
    set_queue_paused(managers[0], "corey", False)
    wait_for(lambda: h3._read(waiting / "state.json"), lambda row: row.get("status") == "completed")


def test_pause_commit_rechecks_after_dispatch_selected_job(queue, monkeypatch):
    from src.video_queue_control import set_queue_paused
    managers, launch, _ = queue
    # Keep the real detached waiter away from dispatch while exercising the
    # selection-to-spawn race directly with its already selected job.
    set_queue_paused(managers[0], "corey", True)
    directory, _, _ = launch()
    spawned = []
    monkeypatch.setattr(h3.subprocess, "Popen", lambda *args, **kwargs: spawned.append(True))
    with (managers[0].root.parent / "test-execution.lock").open("a") as lease:
        assert h3._render(directory / "manifest.json", lease) is False
    assert spawned == [] and h3._read(directory / "state.json")["status"] == "queued"


def test_bulk_queue_operations_check_owner_even_for_explicit_targets(queue):
    from src.video_queue_control import cancel_queued, set_queue_paused
    managers, launch, _ = queue
    set_queue_paused(managers[0], "corey", True)
    directory, _, _ = launch()
    original = (directory / "state.json").read_bytes()
    receipt = cancel_queued(managers[0], "intruder", [{"id": directory.name}], delete=True)
    assert receipt["succeeded"] == 0 and receipt["failed"] == 1
    assert receipt["errors"][0]["error"] == "Video job not found"
    assert (directory / "state.json").read_bytes() == original
    assert not (directory / "cancel").exists()


def test_pause_upgrades_waiters_automatically_and_preserves_other_owner_pause(queue):
    from src.video_queue_control import set_queue_paused, queue_status
    managers, launch, _ = queue
    running, _, _ = launch(hold=True)
    wait_for(lambda: (running / "started").exists(), bool)
    waiting, _, _ = launch(1)
    state = h3._read(waiting / "state.json")
    state.pop("supervisor_control_version")
    h3._write(waiting / "state.json", state)
    set_queue_paused(managers[0], "someone-else", True)
    set_queue_paused(managers[0], "corey", True)
    updated = h3._read(waiting / "state.json")
    assert updated["supervisor"] != state["supervisor"]
    assert updated["supervisor_control_version"] == h3.SUPERVISOR_CONTROL_VERSION
    set_queue_paused(managers[1], "corey", False)
    assert queue_status(managers[0], "someone-else")["paused"]


def test_invalid_scope_and_revision_do_not_change_jobs(queue):
    from src.video_queue_control import cancel_queued, set_queue_paused
    managers, launch, _ = queue
    set_queue_paused(managers[0], "corey", True)
    waiting, _, _ = launch()
    with pytest.raises(ValueError, match="scope"):
        cancel_queued(managers[0], "corey", scope="all-users")
    receipt = cancel_queued(managers[0], "corey", [{"id": waiting.name, "revision": True}])
    assert receipt["failed"] == 1 and receipt["succeeded"] == 0
    assert h3._read(waiting / "state.json")["status"] == "queued"
