"""History pages bound hydration while retaining ownership and queue scope."""
import os
import shutil

import pytest

from src import h3_video as h3
from tests.test_video_submission import backend


def saved_job(manager, index, *, owner="corey", created_at=None, status="completed", recipe=None, legacy=False):
    directory = manager.stage(f"{index:032x}")
    state = {"id": directory.name, "owner": owner, "created_at": created_at if created_at is not None else index,
             "status": status, "phase": status, "filename": directory.name + ".mp4", "steps": 2}
    if status in h3.ACTIVE:
        state["supervisor"] = h3._identity(os.getpid())
    if not legacy:
        state["recipe"] = recipe or "h3"
    config = {"prompt": "Synthetic history test", "steps": 2, "gpu": "test-device"}
    if recipe:
        config.update(recipe=recipe, seed_hunter={"stage": "preview"})
    h3._write(directory / "state.json", state)
    h3._write(directory / "manifest.json", {"config": config})
    h3._write(directory / "progress.json", {"step": 2})
    (directory / "render.log").write_text("Synthetic completed render")
    return directory


@pytest.mark.parametrize("family", ["h3", "bfs"])
def test_pages_are_owned_nonoverlapping_bounded_and_anchored(backend, monkeypatch, family):
    manager = backend.managers[family]
    owned = [saved_job(manager, index) for index in range(1, 24)]
    for index in range(24, 40):
        saved_job(manager, index, owner="other")
    # Symlinked records are never exposed, even when the target is owned.
    (manager.root / ("f" * 32)).symlink_to(owned[0], target_is_directory=True)
    calls, tails = [], []
    original_view, original_tail = manager.view, h3._tail
    def view(identifier, *args, **kwargs):
        calls.append(identifier)
        return original_view(identifier, *args, **kwargs)
    def tail(path):
        tails.append(path.parent.name)
        return original_tail(path)
    monkeypatch.setattr(manager, "view", view)
    monkeypatch.setattr(h3, "_tail", tail)
    client = backend.client()
    first = client.get(f"/api/video/{family}/jobs?page=1&page_size=10").json()
    anchor = first["pagination"]["before"]
    assert first["pagination"] == {"page": 1, "page_size": 10, "total": 23, "total_pages": 3,
                                    "has_next": True, "before": anchor}
    assert calls == tails == [path.name for path in reversed(owned[-10:])]
    newest = saved_job(manager, 40, created_at=anchor + 1)
    seen = [job["id"] for job in first["jobs"]]
    for page, expected in [(2, 10), (3, 3)]:
        calls.clear()
        tails.clear()
        response = client.get(f"/api/video/{family}/jobs", params={"page": page, "page_size": 10, "before": anchor}).json()
        assert len(response["jobs"]) == len(calls) == len(tails) == expected
        assert response["pagination"]["total"] == 23
        assert response["counts"]["completed"] == 24
        assert response["queue"]["counts"]["completed"] == 24
        seen.extend(job["id"] for job in response["jobs"])
    assert seen == [path.name for path in reversed(owned)]
    assert newest.name not in seen
    same = client.get(f"/api/video/{family}/jobs", params={"page": 1, "before": anchor}).json()
    assert [row["id"] for row in same["jobs"]] == seen[:10]
    legacy = client.get(f"/api/video/{family}/jobs").json()
    assert len(legacy["jobs"]) == 24 and "pagination" not in legacy


def test_recipe_filter_precedes_page_and_supports_old_receipts(backend, monkeypatch):
    manager = backend.managers["h3"]
    for index in range(1, 9):
        saved_job(manager, index, legacy=index % 2 == 0)
    hunters = [saved_job(manager, index, recipe="seed_hunter", legacy=index % 2 == 0) for index in range(9, 21)]
    foreign = saved_job(manager, 21, owner="other", recipe="seed_hunter", legacy=True)
    unchanged = {path: path.read_bytes() for directory in manager.root.iterdir()
                 for path in directory.iterdir() if path.is_file()}
    calls = []
    original_read, original_view = h3._read, manager.view
    def read(path, *args, **kwargs):
        assert path != foreign / "manifest.json", "Foreign manifests must not be hydrated for filtering"
        return original_read(path, *args, **kwargs)
    def view(identifier, *args, **kwargs):
        calls.append(identifier)
        return original_view(identifier, *args, **kwargs)
    monkeypatch.setattr(h3, "_read", read)
    monkeypatch.setattr(manager, "view", view)
    client = backend.client()
    page = client.get("/api/video/h3/jobs?page=2&page_size=5&recipe=seed_hunter").json()
    assert page["pagination"]["total"] == page["counts"]["completed"] == 12
    assert len(calls) == 5
    assert [row["id"] for row in page["jobs"]] == [path.name for path in reversed(hunters)][5:10]
    assert all(row["recipe"] == "seed_hunter" for row in page["jobs"])
    assert len(client.get("/api/video/h3/jobs?recipe=seed_hunter").json()["jobs"]) == 12
    assert len(client.get("/api/video/h3/jobs").json()["jobs"]) == 20
    assert all(path.read_bytes() == content for path, content in unchanged.items())


def test_scout_stage_filter_precedes_pagination_and_keeps_history_and_counts_separate(backend, monkeypatch):
    manager = backend.managers['h3']
    stages = {}
    for index in range(1, 22):
        stage = 'scout' if index % 2 else 'preview'
        directory = saved_job(manager, index, recipe='seed_hunter', legacy=index % 3 == 0)
        manifest = h3._read(directory / 'manifest.json')
        manifest['config']['seed_hunter']['stage'] = stage
        h3._write(directory / 'manifest.json', manifest)
        stages[directory.name] = stage
    saved_job(manager, 22)
    foreign = saved_job(manager, 23, owner='other', recipe='seed_hunter', legacy=True)
    original_read = h3._read
    def read(path, *args, **kwargs):
        assert path != foreign / 'manifest.json'
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(h3, '_read', read)
    client = backend.client()
    for stage, total in [('scout', 11), ('not_scout', 10)]:
        reply = client.get('/api/video/h3/jobs', params={'page': 2, 'page_size': 5, 'recipe': 'seed_hunter', 'stage': stage})
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert len(body['jobs']) == 5
        assert body['pagination']['total'] == body['counts']['completed'] == total
        assert all((stages[row['id']] == 'scout') == (stage == 'scout') for row in body['jobs'])
        assert len(client.get('/api/video/h3/jobs', params={'recipe': 'seed_hunter', 'stage': stage}).json()['jobs']) == total
    assert client.get('/api/video/h3/jobs?recipe=seed_hunter&page=1').json()['pagination']['total'] == 21
    assert len(client.get('/api/video/h3/jobs').json()['jobs']) == 22


@pytest.mark.parametrize("family", ["h3", "bfs"])
def test_detail_is_bare_full_job_and_owner_admin_scoped(backend, family):
    manager = backend.managers[family]
    directory = saved_job(manager, 1)
    client = backend.client()
    url = f"/api/video/{family}/jobs/{directory.name}"
    detail = client.get(url)
    assert detail.status_code == 200
    assert detail.json()["id"] == directory.name
    assert detail.json()["prompt"] == "Synthetic history test"
    assert detail.json()["log_tail"] == "Synthetic completed render"
    assert client.get(url, headers={"x-user": "other"}).status_code == 404
    assert client.get(url, headers={"x-user": "reader"}).status_code == 403
    assert client.get(f"/api/video/{family}/jobs?page=1", headers={"x-user": "reader"}).status_code == 403
    assert client.get(f"/api/video/{family}/jobs/not-an-id").status_code == 404


def test_deletion_clamps_last_page_and_equal_timestamps_have_stable_order(backend):
    manager = backend.managers["h3"]
    saved = [saved_job(manager, index, created_at=123) for index in range(1, 13)]
    client = backend.client()
    result = client.get("/api/video/h3/jobs?page=2&page_size=10").json()
    anchor = result["pagination"]["before"]
    assert [job["id"] for job in result["jobs"]] == [saved[1].name, saved[0].name]
    for directory in saved[:2]:
        shutil.rmtree(directory)
    result = client.get("/api/video/h3/jobs", params={"page": 2, "before": anchor}).json()
    assert result["pagination"]["page"] == result["pagination"]["total_pages"] == 1
    assert result["pagination"]["total"] == 10 and not result["pagination"]["has_next"]
    for directory in saved[2:]:
        shutil.rmtree(directory)
    empty = client.get("/api/video/h3/jobs?page=100").json()
    assert empty["jobs"] == []
    assert empty["pagination"]["page"] == empty["pagination"]["total_pages"] == 1
    assert empty["pagination"]["total"] == 0


def test_offpage_dead_worker_reconciles_counts_without_hydration_or_foreign_mutation(backend, monkeypatch):
    manager = backend.managers["h3"]
    saved_job(manager, 1, status="running")
    dead = saved_job(manager, 2, status="queued")
    h3._write(dead / "state.json", {**h3._read(dead / "state.json"), "supervisor": None})
    foreign = saved_job(manager, 3, owner="other", status="queued")
    h3._write(foreign / "state.json", {**h3._read(foreign / "state.json"), "supervisor": None})
    foreign_before = (foreign / "state.json").read_bytes()
    for index in range(4, 54):
        saved_job(manager, index)
    calls, snapshots = [], []
    original_view, original_snapshot = manager.view, h3._snapshot
    def view(identifier, *args, **kwargs):
        calls.append(identifier)
        return original_view(identifier, *args, **kwargs)
    monkeypatch.setattr(manager, "view", view)
    monkeypatch.setattr(h3, "_snapshot", lambda: snapshots.append(True) or original_snapshot())
    result = backend.client().get("/api/video/h3/jobs?page=1").json()
    assert len(calls) == 10 and len(snapshots) == 1
    assert dead.name not in calls
    assert result["counts"] == {"completed": 50, "running": 1, "failed": 1, "stopped": 0, "queued": 0}
    assert result["queue"]["running"] == 1 and result["queue"]["queued"] == 0
    assert h3._read(dead / "state.json")["status"] == "failed"
    assert (foreign / "state.json").read_bytes() == foreign_before


@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=1.5", "page=abc", "page_size=0", "page_size=51",
                                  "page_size=1.5", "page=1&before=nan", "page=1&before=inf",
                                  "page=1&before=-1", "page=1&before=no", "before=1", "recipe=h3",
                                  "stage=scout", "recipe=seed_hunter&stage=preview", "recipe=seed_hunter&stage="])
def test_invalid_pagination_never_hydrates_jobs(backend, monkeypatch, query):
    for manager in backend.managers.values():
        monkeypatch.setattr(manager, "view", lambda *_args, **_kwargs: pytest.fail("Invalid request hydrated history"))
    response = backend.client().get("/api/video/h3/jobs?" + query)
    assert response.status_code == 400
    assert backend.client().get("/api/video/bfs/jobs?recipe=seed_hunter&page=1").status_code == 400
