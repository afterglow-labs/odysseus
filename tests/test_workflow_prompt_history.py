"""Shared prompt history is durable, deduplicated, owned, and independent of jobs."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routes.workflow_prompt_history_routes import setup_workflow_prompt_history_routes
from src.workflow_prompt_history import (
    PromptHistoryStore, PromptTooLarge, config_prompts, compiled_prompts,
    remember_graph_prompts, remember_job_prompts,
)
from tests.test_video_submission import backend, send


@pytest.fixture
def store(tmp_path):
    return PromptHistoryStore(tmp_path / "jobs" / ".prompt_history")


def client(store, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user in {"corey", "other"})
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)
    app.include_router(setup_workflow_prompt_history_routes(store))
    return TestClient(app)


def test_normalization_reuse_sorting_and_persistence(store):
    first = store.record("corey", "  First\r\n  Second\rThird  ", "h3", at=1000)
    same = store.record("corey", "First\n  Second\nThird", "seed_hunter", at=3000)
    assert first["created"] is True and same["created"] is False
    assert same["entry"]["id"] == first["entry"]["id"]
    assert same["entry"]["created_at"] == first["entry"]["created_at"]
    assert same["entry"]["workflow"] == "seed_hunter"
    assert same["entry"]["last_used_at"] > first["entry"]["last_used_at"]
    store.record("corey", "first\n  Second\nThird", "h3", at=2000)  # Case is significant.
    store.record("corey", "First\n Second\nThird", "h3", at=1500)  # Internal spacing is significant.
    result = PromptHistoryStore(store.root).list("corey")
    assert result["total"] == 3
    assert result["prompts"][0] == same["entry"]
    assert all(row["created_at"].endswith("Z") for row in result["prompts"])
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_concurrent_saves_have_one_stable_entry(store):
    def save(index):
        return PromptHistoryStore(store.root).record("corey", "Shared prompt", "h3", at=1000 + index)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, range(24)))
    assert sum(row["created"] for row in results) == 1
    assert len({row["entry"]["id"] for row in results}) == 1
    assert store.list("corey")["total"] == 1


def test_no_retention_cap_and_literal_owner_scoped_search(store):
    store.record_many("corey", [(f"Prompt {index}", "h3") for index in range(550)], at=1000)
    store.record("corey", "100%_quoted' text", "bfs", at=2000)
    store.record("other", "PRIVATE 100%_quoted' text", "h3", at=3000)
    first = store.list("corey", limit=200)
    second = store.list("corey", limit=200, offset=first["next_offset"])
    last = store.list("corey", limit=200, offset=second["next_offset"])
    assert first["total"] == 551 and len(last["prompts"]) == 151 and last["next_offset"] is None
    assert len({row["id"] for page in (first, second, last) for row in page["prompts"]}) == 551
    assert [row["prompt"] for row in store.list("corey", q="%_QUOTED'")["prompts"]] == ["100%_quoted' text"]
    assert store.list("nobody")["total"] == 0


def saved(root, owner, config=None, *, compiled=None, at=1000):
    directory = root / uuid.uuid4().hex
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps({"owner": owner, "created_at": at, "status": "failed"}))
    (directory / "manifest.json").write_text(json.dumps({"compiled": compiled} if compiled else {"config": config}))
    return directory


def test_backfill_once_preserves_owner_bfs_notes_and_old_timestamps(store, tmp_path, monkeypatch):
    jobs = store.root.parent
    own = saved(jobs / "h3", "corey", {"prompt": "Shared", "recipe": "seed_hunter"}, at=1000)
    saved(jobs / "h3", "corey", {"prompt": "Shared"}, at=2000)
    saved(jobs / "h3", "other", {"prompt": "Someone else's prompt"})
    saved(jobs / "h3", None, {"prompt": "Ownerless prompt"})
    saved(jobs / "bfs", "corey", {"workflow_id": "head", "prompt": "WRAPPED instructions", "prompt_notes": "Only editable notes"})
    saved(jobs / "bfs", "corey", {"workflow_id": "head", "prompt": "WRAPPED default", "prompt_notes": ""})
    saved(jobs / "bfs", "corey", {"workflow_id": "head", "prompt": "Legacy wrapper without notes"})
    outside = saved(tmp_path / "outside", "corey", {"prompt": "Never follow links"})
    (jobs / "h3" / uuid.uuid4().hex).symlink_to(outside, target_is_directory=True)
    escaped = saved(jobs / "h3", "corey", {})
    (escaped / "manifest.json").unlink()
    (escaped / "manifest.json").symlink_to(outside / "manifest.json")
    graphs = tmp_path / "graphs"
    saved(graphs, "corey", compiled={"plan": [{"family": "h3", "config": {"prompt": "Graph prompt"}}]})
    store.graph_root = graphs
    recent = store.record("corey", "Shared", "seed_scout", at=4000)["entry"]
    result = store.list("corey")
    assert {row["prompt"] for row in result["prompts"]} == {"Shared", "Only editable notes", "Graph prompt"}
    merged = result["prompts"][0]
    assert merged["id"] == recent["id"] and merged["workflow"] == "seed_scout"
    assert merged["created_at"] < recent["created_at"] and merged["last_used_at"] == recent["last_used_at"]
    shutil.rmtree(own)
    monkeypatch.setattr(store, "_historical", lambda owner: pytest.fail("History rescanned on a repeated read"))
    assert store.list("corey")["total"] == 3
    assert PromptHistoryStore(store.root).list("corey")["total"] == 3


def test_main_prompt_extraction_ignores_negative_dialogue_and_bfs_templates():
    graph = {
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": "Main graph text"}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": "Bad anatomy"}},
        "sampler": {"class_type": "KSampler", "inputs": {"positive": ["positive", 0], "negative": ["negative", 0]}},
        "unused": {"class_type": "String", "inputs": {"text": "Unused draft"}},
    }
    assert config_prompts({"recipe": "comfy_graph", "prompt": graph}) == [("Main graph text", "workflow_graph")]
    graph["negative"]["inputs"] = {"prompt": "Negative encoder alias"}
    assert config_prompts({"recipe": "comfy_graph", "prompt": graph}) == [("Main graph text", "workflow_graph")]
    assert config_prompts({"prompt": "Main", "negative_prompt": "No", "dialogue": "Fragment"}) == [("Main", "h3")]
    assert config_prompts({"prompt": "Scout", "recipe": "seed_hunter", "seed_hunter": {"stage": "scout"}}) == [("Scout", "seed_scout")]
    assert config_prompts({"prompt": "Wrapper", "workflow_id": "test"}) == []
    assert compiled_prompts({"plan": [{"family": "bfs", "config": {"prompt": "Editable notes"}}]}) == [("Editable notes", "bfs")]
    graph["positive"]["inputs"]["text"] = ["positive", 0]  # Malformed cycles do not recurse forever.
    assert config_prompts({"prompt": graph}) == []


def test_routes_authentication_persistence_and_owner_isolation(store, monkeypatch):
    api = client(store, monkeypatch)
    path = "/api/workflows/prompt-history"
    assert api.get(path).status_code == 403
    assert api.post(path, json={"prompt": "x"}).status_code == 403
    assert api.get(path, headers={"x-user": "reader"}).status_code == 403
    first = api.post(path, headers={"x-user": "corey"}, json={"prompt": " Line one\r\nLine two ", "workflow": "h3"})
    assert first.status_code == 200 and first.json()["created"] is True
    same = api.post(path, headers={"x-user": "corey"}, json={"prompt": "Line one\nLine two", "workflow": "bfs"})
    assert same.status_code == 200 and same.json()["created"] is False
    assert same.json()["entry"]["id"] == first.json()["entry"]["id"]
    assert api.get(path, headers={"x-user": "other"}).json()["total"] == 0
    restored = client(PromptHistoryStore(store.root), monkeypatch)
    assert restored.get(path + "?q=LINE&limit=1", headers={"x-user": "corey"}).json()["prompts"] == [same.json()["entry"]]


@pytest.mark.parametrize("body,status", [
    ({"prompt": " "}, 400), ({"prompt": {}}, 400), ({"prompt": "ok", "workflow": []}, 400),
    ({"prompt": "ok", "owner": "other"}, 400), ([], 400),
    ({"prompt": "x" * (65536 + 1)}, 413), ({"prompt": "\ud800"}, 400),
])
def test_route_validation(body, status, store, monkeypatch):
    response = client(store, monkeypatch).post("/api/workflows/prompt-history", headers={"x-user": "corey"}, content=json.dumps(body).encode())
    assert response.status_code == status, response.text


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=wat", "offset=-1", "offset=2147483648", "q=" + "a" * 1001])
def test_get_validation(query, store, monkeypatch):
    assert client(store, monkeypatch).get("/api/workflows/prompt-history?" + query, headers={"x-user": "corey"}).status_code == 400


def test_storage_failure_is_explicit_for_manual_save_but_nonfatal_for_accepted_jobs(store, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic unavailable database")
    monkeypatch.setattr(PromptHistoryStore, "record", fail)
    monkeypatch.setattr(PromptHistoryStore, "record_many", fail)
    response = client(store, monkeypatch).post("/api/workflows/prompt-history", headers={"x-user": "corey"}, json={"prompt": "Saved"})
    assert response.status_code == 503
    remember_job_prompts(SimpleNamespace(root=store.root.parent / "h3"), "corey", [{"prompt": "Accepted"}])
    remember_graph_prompts(SimpleNamespace(root=store.root.parent / "runs", managers={}), "corey", {"plan": [{"family": "h3", "config": {"prompt": "Accepted"}}]})


def test_symlink_database_rejected(store, tmp_path):
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"unchanged")
    store.root.mkdir(parents=True)
    store.path.symlink_to(outside)
    with pytest.raises(OSError):
        store.record("corey", "x")
    assert outside.read_bytes() == b"unchanged"


@pytest.mark.parametrize("family", ["h3", "bfs"])
def test_real_submission_edit_bulk_and_retry_hooks_share_one_library(backend, family):
    from tests.test_video_job_edit import patch
    from tests.test_video_queue_bulk import request as bulk_request
    store = PromptHistoryStore(backend.managers[family].root.parent / ".prompt_history")
    # Mark migration complete before submitting to prove hooks, not backfill.
    assert store.list("corey")["total"] == 0
    receipt = str(uuid.uuid4())
    first = send(backend, family, receipt)
    assert first.status_code == 201, first.text
    second = send(backend, family)
    assert second.status_code == 201
    assert send(backend, family, receipt).status_code == 201
    result = store.list("corey")
    assert result["total"] == 1 and result["prompts"][0]["prompt"] == "Same exact prompt."
    edited = patch(backend, first.json(), family, config={**backend.configs[family], "prompt": "One edited prompt"})
    assert edited.status_code == 200, edited.text
    assert store.list("corey")["total"] == 2
    bulk = bulk_request(backend, "edit", [edited.json(), second.json()], {"prompt": "Shared batch revision"}, family=family)
    assert bulk.status_code == 200, bulk.text
    assert [row["prompt"] for row in store.list("corey")["prompts"]] == ["Shared batch revision", "One edited prompt", "Same exact prompt."]
    assert store.list("other")["total"] == 0
    # A stale edit cannot leak its rejected prompt into reusable history.
    rejected = patch(backend, first.json(), family, config={**backend.configs[family], "prompt": "Rejected"})
    assert rejected.status_code == 409
    assert store.list("corey")["total"] == 3


def test_job_still_accepted_when_history_write_fails(backend, monkeypatch):
    monkeypatch.setattr(PromptHistoryStore, "record_many", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    response = send(backend, "h3")
    assert response.status_code == 201, response.text
    assert len(backend.calls) == 1


def test_failed_launch_does_not_record_prompt(backend, monkeypatch):
    from src import h3_video
    store = PromptHistoryStore(backend.managers["h3"].root.parent / ".prompt_history")
    assert store.list("corey")["total"] == 0
    def fail(*args, **kwargs):
        raise OSError("synthetic spawn failure")
    monkeypatch.setattr(h3_video.subprocess, "Popen", fail)
    response = send(backend, "h3")
    assert response.status_code >= 400
    assert store.list("corey")["total"] == 0


def test_database_connection_closes_when_initialization_fails(store, monkeypatch):
    closed = []
    class BrokenDatabase:
        row_factory = None
        def executescript(self, script):
            raise sqlite3.OperationalError("failed schema setup")
        def close(self):
            closed.append(True)
    store.root.mkdir(parents=True)
    store.path.touch()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: BrokenDatabase())
    with pytest.raises(sqlite3.OperationalError):
        store.record("corey", "Fixture")
    assert closed == [True]


def test_graph_accept_records_native_prompt_before_execution_in_same_store(backend, tmp_path, monkeypatch):
    from src.workflow_graph_runs import GraphRunManager
    from tests.test_workflow_graph_runs import native_plan, staged
    store = PromptHistoryStore(backend.managers["h3"].root.parent / ".prompt_history")
    assert store.list("corey")["total"] == 0
    manager = GraphRunManager(tmp_path / "graph_runs", backend.managers)
    monkeypatch.setattr(manager, "_start", lambda identity: None)
    result = manager.accept("corey", {"title": "Fixture"}, native_plan(backend), staged(tmp_path), str(uuid.uuid4()))
    assert result["status"] == "queued" and backend.calls == []
    assert store.list("corey")["total"] == 1
    assert store.list("corey")["prompts"][0]["prompt"] == "Same exact prompt."
