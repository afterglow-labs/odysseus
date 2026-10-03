"""Daybreak remains a conversation preference across reloads and route changes."""

import json

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Query, sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as database
import core.session_manager as managers
import routes.session_routes as routes
import routes.history.history_routes as history_routes
from core.models import ChatMessage

CHATGPT = "https://chatgpt.com/backend-api/codex/responses"


@pytest.fixture
def state(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(managers, "SessionLocal", factory)
    monkeypatch.setattr(routes, "SessionLocal", factory)
    monkeypatch.setattr(history_routes, "SessionLocal", factory)
    monkeypatch.setattr(routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(routes, "_verify_session_owner", lambda *args: None)
    monkeypatch.setattr(history_routes, "_verify_session_owner", lambda *args: None)
    monkeypatch.setattr(routes, "_reject_raw_endpoint_url_for_non_admin", lambda *args: None)
    monkeypatch.setattr("src.llm_core.list_model_ids", lambda *args, **kwargs: ["gpt-6-sol", "local"])
    monkeypatch.setattr("src.event_bus.fire_event", lambda *args, **kwargs: None)
    manager = managers.SessionManager()
    monkeypatch.setattr("core.models._SESSION_MANAGER_INSTANCE", manager)
    # setup_session_routes registers closures on a module-level router. Give
    # each fixture its own router so neither clients nor later tests reuse a
    # previous fixture's session manager.
    monkeypatch.setattr(routes, "router", APIRouter(
        prefix="/api", dependencies=list(routes.router.dependencies),
    ))
    app = FastAPI()
    app.include_router(routes.setup_session_routes(manager, {}))
    app.include_router(history_routes.setup_history_routes(manager))
    yield manager, factory, TestClient(app)
    engine.dispose()


def test_choice_survives_metadata_load_history_load_and_worker_refresh(state):
    manager, factory, _ = state
    session = manager.create_session("one", "Example", CHATGPT, "gpt-6-sol", daybreak_enabled=True)
    assert session.daybreak_enabled is True
    manager.sessions.clear()
    assert manager.get_session("one").daybreak_enabled is True
    manager.set_daybreak_enabled("one", False)
    assert manager.get_session("one").daybreak_enabled is False
    with factory() as db:
        stored = db.get(database.Session, "one")
        stored.daybreak_enabled = True
        db.commit()
    assert manager.get_session("one").daybreak_enabled is True


def test_checkbox_patch_keeps_same_model_and_rejects_other_providers(state):
    manager, _, client = state
    manager.create_session("one", "Example", CHATGPT, "gpt-6-sol")
    checked = client.patch("/api/session/one", data={"daybreak_enabled": "true"})
    assert checked.status_code == 200, checked.text
    assert checked.json()["daybreak_enabled"] is True
    assert manager.get_session("one").model == "gpt-6-sol"
    unchecked = client.patch("/api/session/one", data={"daybreak_enabled": "false"})
    assert unchecked.status_code == 200
    assert manager.get_session("one").daybreak_enabled is False
    manager.create_session("two", "Local", "http://localhost:8000/v1", "local")
    rejected = client.patch("/api/session/two", data={"daybreak_enabled": "true"})
    assert rejected.status_code == 400
    assert manager.get_session("two").daybreak_enabled is False


def test_provider_switch_resets_daybreak_and_invalid_switch_is_not_applied(state):
    manager, _, client = state
    manager.create_session("one", "Example", CHATGPT, "gpt-6-sol", daybreak_enabled=True)
    other = {"model": "local", "endpoint_url": "http://localhost:8000/v1"}
    rejected = client.patch("/api/session/one", data={**other, "daybreak_enabled": "true"})
    assert rejected.status_code == 400
    assert manager.sessions["one"].model == "gpt-6-sol"
    switched = client.patch("/api/session/one", data=other)
    assert switched.status_code == 200, switched.text
    assert switched.json()["daybreak_enabled"] is False
    assert manager.get_session("one").daybreak_enabled is False


@pytest.mark.parametrize("enabled", [None, "false", "true"])
def test_create_and_history_routes_round_trip_daybreak_after_reload(state, enabled):
    manager, factory, client = state
    form = {"name": "Created via API", "endpoint_url": CHATGPT, "model": "gpt-6-sol"}
    if enabled is not None:
        form["daybreak_enabled"] = enabled
    response = client.post("/api/session", data=form)
    assert response.status_code == 200, response.text
    payload = response.json()
    expected = enabled == "true"
    assert payload["daybreak_enabled"] is expected
    session_id = payload["id"]
    manager.add_message(session_id, ChatMessage("user", "fixture message"))
    manager.sessions.clear()
    for suffix in ("", "?limit=1"):
        history = client.get(f"/api/history/{session_id}{suffix}")
        assert history.status_code == 200, history.text
        assert history.json()["daybreak_enabled"] is expected
        assert history.json()["history"][0]["content"] == "fixture message"
    with factory() as db:
        assert db.get(database.Session, session_id).to_dict()["daybreak_enabled"] is expected


def test_create_rejects_daybreak_for_other_provider_without_creating_session(state):
    _, factory, client = state
    response = client.post("/api/session", data={
        "name": "Rejected", "model": "local", "endpoint_url": "http://localhost:8000/v1",
        "daybreak_enabled": "true",
    })
    assert response.status_code == 400
    with factory() as db:
        assert db.query(database.Session).count() == 0


def test_fork_preserves_daybreak_and_remains_independent_after_reload(state):
    manager, factory, client = state
    response = client.post("/api/session", data={
        "name": "Original", "model": "gpt-6-sol", "endpoint_url": CHATGPT,
        "daybreak_enabled": "true",
    })
    assert response.status_code == 200, response.text
    original_id = response.json()["id"]
    manager.add_message(original_id, ChatMessage("user", "copy this"))
    manager.sessions.clear()
    fork = client.post(f"/api/session/{original_id}/fork", json={"keep_count": 1})
    assert fork.status_code == 200, fork.text
    fork_id = fork.json()["id"]
    manager.sessions.clear()
    history = client.get(f"/api/history/{fork_id}?limit=1").json()
    assert history["daybreak_enabled"] is True
    assert history["history"][0]["content"] == "copy this"
    assert client.patch(f"/api/session/{fork_id}", data={"daybreak_enabled": "false"}).status_code == 200
    with factory() as db:
        assert db.get(database.Session, original_id).daybreak_enabled is True
        assert db.get(database.Session, fork_id).daybreak_enabled is False


def test_rejected_daybreak_patch_does_not_rename_or_move_session(state):
    manager, factory, client = state
    manager.create_session("one", "Original", CHATGPT, "gpt-6-sol", daybreak_enabled=True)
    response = client.patch("/api/session/one", data={
        "name": "Must not stick", "folder": "new-folder", "model": "local",
        "endpoint_url": "http://localhost:8000/v1", "daybreak_enabled": "true",
    })
    assert response.status_code == 400
    assert manager.sessions["one"].name == "Original"
    with factory() as db:
        stored = db.get(database.Session, "one")
        assert stored.name == "Original"
        assert stored.folder is None
        assert stored.endpoint_url == CHATGPT
        assert stored.daybreak_enabled is True


def test_failed_patch_commit_keeps_database_and_cached_provider_unchanged(state, monkeypatch):
    manager, factory, client = state
    manager.create_session("one", "Original", CHATGPT, "gpt-6-sol", daybreak_enabled=True)

    def failed_session():
        db = factory()
        def fail():
            raise RuntimeError("simulated commit failure")
        db.commit = fail
        return db

    monkeypatch.setattr(routes, "SessionLocal", failed_session)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        client.patch("/api/session/one", data={
            "name": "Must not stick", "folder": "new-folder", "model": "local",
            "endpoint_url": "http://localhost:8000/v1",
        })
    cached = manager.sessions["one"]
    assert (cached.name, cached.model, cached.endpoint_url, cached.daybreak_enabled) == ("Original", "gpt-6-sol", CHATGPT, True)
    with factory() as db:
        stored = db.get(database.Session, "one")
        assert (stored.name, stored.folder, stored.model, stored.endpoint_url, stored.daybreak_enabled) == ("Original", None, "gpt-6-sol", CHATGPT, True)


def test_toggle_rechecks_provider_changed_by_another_worker(state, monkeypatch):
    manager, factory, client = state
    manager.create_session("one", "Original", CHATGPT, "gpt-6-sol")

    def after_provider_change():
        with factory() as db:
            stored = db.get(database.Session, "one")
            stored.endpoint_url = "http://localhost:8000/v1"
            stored.model = "local"
            db.commit()
        return factory()

    monkeypatch.setattr(routes, "SessionLocal", after_provider_change)
    response = client.patch("/api/session/one", data={"daybreak_enabled": "true"})
    assert response.status_code == 400
    with factory() as db:
        assert db.get(database.Session, "one").daybreak_enabled is False


def test_toggle_guards_provider_change_between_validation_and_write(state, monkeypatch):
    manager, factory, client = state
    manager.create_session("one", "Original", CHATGPT, "gpt-6-sol")

    class ProviderChangesBeforeWrite(Query):
        def update(self, *args, **kwargs):
            with factory() as db:
                stored = db.get(database.Session, "one")
                stored.endpoint_url = "http://localhost:8000/v1"
                stored.model = "local"
                db.commit()
            return super().update(*args, **kwargs)

    monkeypatch.setattr(routes, "SessionLocal", lambda: factory(query_cls=ProviderChangesBeforeWrite))
    response = client.patch("/api/session/one", data={"name": "Must not stick", "daybreak_enabled": "true"})
    assert response.status_code == 409, response.text
    with factory() as db:
        stored = db.get(database.Session, "one")
        assert stored.name == "Original"
        assert stored.model == "local"
        assert stored.daybreak_enabled is False


def test_existing_database_migration_is_repeatable_and_keeps_selected_state(monkeypatch):
    engine = create_engine("sqlite://")
    monkeypatch.setattr(database, "engine", engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sessions (id TEXT PRIMARY KEY)"))
        conn.execute(text("INSERT INTO sessions VALUES ('old')"))
    database._migrate_add_daybreak_enabled_column()
    with engine.begin() as conn:
        assert conn.execute(text("SELECT daybreak_enabled FROM sessions")).scalar() == 0
        conn.execute(text("UPDATE sessions SET daybreak_enabled = 1"))
    database._migrate_add_daybreak_enabled_column()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT daybreak_enabled FROM sessions")).scalar() == 1
    engine.dispose()


@pytest.mark.parametrize(("effort", "expected"), [(None, None), ("", None), (" ULTRA ", "ultra")])
def test_reasoning_effort_create_history_and_fork_round_trip(state, effort, expected):
    manager, factory, client = state
    form = {"model": "gpt-6-sol", "endpoint_url": CHATGPT, "daybreak_enabled": "true"}
    if effort is not None:
        form["reasoning_effort"] = effort
    response = client.post("/api/session", data=form)
    assert response.status_code == 200, response.text
    sid = response.json()["id"]
    assert response.json()["reasoning_effort"] == expected
    manager.add_message(sid, ChatMessage("user", "effort fixture"))
    manager.sessions.clear()
    for suffix in ("", "?limit=1"):
        history = client.get(f"/api/history/{sid}{suffix}").json()
        assert history["reasoning_effort"] == expected
        assert history["daybreak_enabled"] is True
    assert next(row for row in client.get("/api/sessions").json() if row["id"] == sid)["reasoning_effort"] == expected
    fork = client.post(f"/api/session/{sid}/fork", json={"keep_count": 1})
    assert fork.status_code == 200, fork.text
    manager.sessions.clear()
    copied = manager.get_session(fork.json()["id"])
    assert copied.reasoning_effort == expected
    assert copied.daybreak_enabled is True
    with factory() as db:
        assert db.get(database.Session, sid).to_dict()["reasoning_effort"] == expected


def test_reasoning_effort_patch_distinguishes_omission_clear_and_daybreak(state):
    manager, factory, client = state
    manager.create_session("one", "Original", CHATGPT, "gpt-6-sol", daybreak_enabled=True, reasoning_effort="high")
    assert client.patch("/api/session/one", data={"name": "Renamed"}).status_code == 200
    assert manager.get_session("one").reasoning_effort == "high"
    cleared = client.patch("/api/session/one", data={"reasoning_effort": ""})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["reasoning_effort"] is None
    assert manager.get_session("one").daybreak_enabled is True
    selected = client.patch("/api/session/one", data={"reasoning_effort": "xhigh", "daybreak_enabled": "false"})
    assert selected.status_code == 200, selected.text
    assert manager.get_session("one").reasoning_effort == "xhigh"
    assert manager.get_session("one").daybreak_enabled is False
    manager.sessions.clear()
    assert manager.get_session("one").reasoning_effort == "xhigh"
    manager.set_reasoning_effort("one", None)
    with factory() as db:
        assert db.get(database.Session, "one").reasoning_effort is None


def test_reasoning_effort_rejects_other_providers_atomically_and_clears_on_switch(state):
    manager, factory, client = state
    manager.create_session("one", "Original", CHATGPT, "gpt-6-sol", daybreak_enabled=True, reasoning_effort="high")
    other = {"model": "local", "endpoint_url": "http://localhost:8000/v1"}
    rejected = client.patch("/api/session/one", data={**other, "name": "Must not stick", "reasoning_effort": "high"})
    assert rejected.status_code == 400, rejected.text
    with factory() as db:
        stored = db.get(database.Session, "one")
        assert (stored.name, stored.model, stored.reasoning_effort, stored.daybreak_enabled) == ("Original", "gpt-6-sol", "high", True)
    switched = client.patch("/api/session/one", data=other)
    assert switched.status_code == 200, switched.text
    assert switched.json()["reasoning_effort"] is None
    assert switched.json()["daybreak_enabled"] is False
    assert client.post("/api/session", data={**other, "reasoning_effort": "high"}).status_code == 400


def _add_effort_catalog(factory, monkeypatch, *, account="account-a", endpoint="endpoint-a", models=None):
    import src.chatgpt_capabilities as capabilities
    monkeypatch.setattr(capabilities, "_database_handles", lambda: (database.ProviderAuthSession, factory))
    models = models or {"gpt-6-sol": ["low", "high"], "other-model": ["low"]}
    catalog = {"revision": 1, "models": {}, "reasoning_models": {
        model: {"supported": True, "levels": levels, "default": levels[0]} for model, levels in models.items()
    }}
    with factory() as db:
        db.add(database.ProviderAuthSession(id=account, provider="chatgpt-subscription", owner="alice",
            base_url=CHATGPT.removesuffix("/responses"), model_capabilities=json.dumps(catalog)))
        db.add(database.ModelEndpoint(id=endpoint, name=endpoint, owner="alice", is_enabled=True,
            base_url=CHATGPT.removesuffix("/responses"), provider_auth_id=account))
        db.commit()


def test_reasoning_effort_known_catalog_validates_exact_account_without_network(state, monkeypatch):
    _, factory, client = state
    _add_effort_catalog(factory, monkeypatch)
    _add_effort_catalog(factory, monkeypatch, account="account-b", endpoint="endpoint-b", models={"gpt-6-sol": ["ultra"]})
    form = {"model": "gpt-6-sol", "endpoint_id": "endpoint-a", "reasoning_effort": "ultra"}
    rejected = client.post("/api/session", data=form)
    assert rejected.status_code == 400, rejected.text
    assert "not supported" in rejected.json()["detail"]
    with factory() as db:
        assert db.query(database.Session).count() == 0
    accepted = client.post("/api/session", data={**form, "endpoint_id": "endpoint-b"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["reasoning_effort"] == "ultra"


def test_reasoning_effort_model_switch_resets_inherited_incompatible_choice(state, monkeypatch):
    manager, factory, client = state
    _add_effort_catalog(factory, monkeypatch)
    manager.create_session("one", "Original", CHATGPT, "gpt-6-sol", daybreak_enabled=True, reasoning_effort="high")
    target = {"model": "other-model", "endpoint_url": CHATGPT, "endpoint_id": "endpoint-a"}
    rejected = client.patch("/api/session/one", data={**target, "name": "Must not stick", "reasoning_effort": "high"})
    assert rejected.status_code == 400, rejected.text
    assert manager.get_session("one").name == "Original"
    accepted = client.patch("/api/session/one", data=target)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["reasoning_effort"] is None
    assert accepted.json()["daybreak_enabled"] is True


def test_reasoning_effort_write_rejects_a_concurrent_model_switch(state, monkeypatch):
    manager, factory, client = state
    manager.create_session("one", "Original", CHATGPT, "gpt-6-sol", reasoning_effort="low")

    class ModelChangesBeforeWrite(Query):
        def update(self, *args, **kwargs):
            with factory() as db:
                stored = db.get(database.Session, "one")
                stored.model = "different-model"
                db.commit()
            return super().update(*args, **kwargs)

    monkeypatch.setattr(routes, "SessionLocal", lambda: factory(query_cls=ModelChangesBeforeWrite))
    response = client.patch("/api/session/one", data={"name": "Must not stick", "reasoning_effort": "high"})
    assert response.status_code == 409, response.text
    with factory() as db:
        stored = db.get(database.Session, "one")
        assert (stored.name, stored.reasoning_effort) == ("Original", "low")


def test_reasoning_effort_migration_preserves_daybreak_and_default(monkeypatch):
    engine = create_engine("sqlite://")
    monkeypatch.setattr(database, "engine", engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sessions (id TEXT PRIMARY KEY, daybreak_enabled BOOLEAN NOT NULL DEFAULT FALSE)"))
        conn.execute(text("INSERT INTO sessions VALUES ('old', TRUE)"))
    database._migrate_add_reasoning_effort_column()
    with engine.begin() as conn:
        assert tuple(conn.execute(text("SELECT daybreak_enabled, reasoning_effort FROM sessions")).one()) == (1, None)
        conn.execute(text("UPDATE sessions SET reasoning_effort = 'high'"))
    database._migrate_add_reasoning_effort_column()
    with engine.connect() as conn:
        assert tuple(conn.execute(text("SELECT daybreak_enabled, reasoning_effort FROM sessions")).one()) == (1, "high")
    engine.dispose()


def test_chat_request_preserves_explicit_default_effort():
    from src.request_models import ChatRequest
    assert ChatRequest(message="hello", session="one").reasoning_effort is None
    assert ChatRequest(message="hello", session="one", reasoning_effort="").reasoning_effort == ""
