"""Reconnect never replaces a different provider account's credentials."""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import core.database as database
import routes.chatgpt_subscription_routes as routes


@pytest.fixture
def connections(monkeypatch):
    engine = create_engine("sqlite://")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(routes, "SessionLocal", factory)
    monkeypatch.setattr(routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(routes.chatgpt_subscription, "fetch_available_models", lambda token: ["gpt-6-sol"])
    monkeypatch.setattr("src.chatgpt_capabilities.bind_account_token", lambda *args: None)
    with factory() as db:
        for suffix, owner in (("one", "alice"), ("two", "alice"), ("other", "bob")):
            db.add(database.ProviderAuthSession(
                id=f"auth-{suffix}", owner=owner, provider="chatgpt-subscription",
                base_url=routes.chatgpt_subscription.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL,
                access_token=f"old-{suffix}", refresh_token=f"refresh-{suffix}",
                model_capabilities='{"models":{"gpt-6-sol":{"supported":false}}}',
            ))
            db.add(database.ModelEndpoint(
                id=suffix, name=f"Custom {suffix}", owner=owner,
                base_url=routes.chatgpt_subscription.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL,
                provider_auth_id=f"auth-{suffix}",
            ))
        db.commit()
    yield factory
    engine.dispose()


def test_reconnect_updates_exact_account_and_preserves_endpoint_name(connections):
    result = routes._provision_endpoint(
        {"access_token": "new-token", "refresh_token": "new-refresh"}, "alice",
        endpoint_id="two", expected_auth_id="auth-two",
    )
    assert result["id"] == "two"
    assert result["name"] == "Custom two"
    with connections() as db:
        assert db.get(database.ProviderAuthSession, "auth-one").access_token == "old-one"
        assert db.get(database.ProviderAuthSession, "auth-two").access_token == "new-token"
        assert db.get(database.ProviderAuthSession, "auth-two").model_capabilities is None
        assert db.query(database.ProviderAuthSession).count() == 3
        assert db.query(database.ModelEndpoint).count() == 3


@pytest.mark.parametrize("target,expected_auth,status", [
    ("other", "auth-other", 404), ("missing", "missing", 404), ("two", "auth-one", 409),
])
def test_reconnect_rejects_wrong_owner_missing_or_changed_connection(connections, monkeypatch, target, expected_auth, status):
    monkeypatch.setattr(routes.chatgpt_subscription, "fetch_available_models", lambda token: pytest.fail("unauthorized discovery"))
    with pytest.raises(HTTPException) as error:
        routes._provision_endpoint(
            {"access_token": "new", "refresh_token": "new"}, "alice",
            endpoint_id=target, expected_auth_id=expected_auth,
        )
    assert error.value.status_code == status
    with connections() as db:
        assert db.get(database.ProviderAuthSession, "auth-two").access_token == "old-two"


def test_start_validates_and_captures_exact_reconnect_target(connections, monkeypatch):
    calls = []
    def device_code():
        calls.append(True)
        return {"device_auth_id": "device", "user_code": "code"}
    monkeypatch.setattr(routes.chatgpt_subscription, "request_device_code", device_code)
    start = routes._start_device_flow(object(), {"endpoint_id": "two"})
    assert start.pending["endpoint_id"] == "two"
    assert start.pending["expected_auth_id"] == "auth-two"
    with pytest.raises(HTTPException):
        routes._start_device_flow(object(), {"endpoint_id": "other"})
    assert len(calls) == 1


def test_poll_does_not_exchange_another_users_device_code(connections, monkeypatch):
    monkeypatch.setattr(routes.chatgpt_subscription, "poll_device_auth", lambda *args: pytest.fail("cross-user poll"))
    with pytest.raises(HTTPException) as error:
        routes._poll_device_flow(object(), {"owner": "bob"})
    assert error.value.status_code == 403


def test_poll_passes_original_reconnect_target(connections, monkeypatch):
    monkeypatch.setattr(routes.chatgpt_subscription, "poll_device_auth", lambda *args: {"authorization_code": "code", "code_verifier": "verifier"})
    monkeypatch.setattr(routes.chatgpt_subscription, "exchange_authorization_code", lambda *args: {"access_token": "new", "refresh_token": "new"})
    result = routes._poll_device_flow(object(), {
        "owner": "alice", "device_auth_id": "device", "user_code": "code",
        "endpoint_id": "two", "expected_auth_id": "auth-two",
    })
    assert result.status == "authorized"
    assert result.endpoint["id"] == "two"


def test_reconnect_rechecks_target_after_discovery(connections, monkeypatch):
    def discover(token):
        with connections() as db:
            db.get(database.ModelEndpoint, "two").provider_auth_id = "auth-one"
            db.commit()
        return ["gpt-6-sol"]
    monkeypatch.setattr(routes.chatgpt_subscription, "fetch_available_models", discover)
    with pytest.raises(HTTPException) as error:
        routes._provision_endpoint(
            {"access_token": "new", "refresh_token": "new"}, "alice",
            endpoint_id="two", expected_auth_id="auth-two",
        )
    assert error.value.status_code == 409
    with connections() as db:
        assert db.get(database.ProviderAuthSession, "auth-two").access_token == "old-two"
        assert db.get(database.ProviderAuthSession, "auth-one").access_token == "old-one"


def test_capability_migration_preserves_existing_auth_and_is_idempotent(monkeypatch):
    engine = create_engine("sqlite://")
    monkeypatch.setattr(database, "engine", engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE provider_auth_sessions (id TEXT PRIMARY KEY, access_token TEXT)"))
        conn.execute(text("INSERT INTO provider_auth_sessions VALUES ('one', 'encrypted-placeholder')"))
    database._migrate_add_provider_model_capabilities_column()
    with engine.begin() as conn:
        assert "model_capabilities" in {c["name"] for c in inspect(conn).get_columns("provider_auth_sessions")}
        assert conn.execute(text("SELECT access_token, model_capabilities FROM provider_auth_sessions")).one() == ("encrypted-placeholder", None)
        conn.execute(text("UPDATE provider_auth_sessions SET model_capabilities = '{}'"))
    database._migrate_add_provider_model_capabilities_column()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT model_capabilities FROM provider_auth_sessions")).scalar_one() == "{}"
    engine.dispose()
