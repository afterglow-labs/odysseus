"""Daybreak catalog and denials belong to the connected account, not model names."""

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ProviderAuthSession
from src import chatgpt_capabilities as caps
from src import chatgpt_subscription as subscription


@pytest.fixture
def accounts(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'capabilities.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(caps, "_database_handles", lambda: (ProviderAuthSession, sessions))
    with sessions() as db:
        for account in ("alice", "bob"):
            db.add(ProviderAuthSession(id=account, provider="chatgpt-subscription", owner=account,
                                       base_url=subscription.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL))
        db.commit()
    caps._token_accounts.clear()
    caps._pending_catalogs.clear()
    caps.bind_account_token("alice", "alice-token")
    caps.bind_account_token("bob", "bob-token")
    yield sessions
    caps._token_accounts.clear()
    caps._pending_catalogs.clear()
    engine.dispose()


@pytest.mark.parametrize("metadata,expected", [
    (None, None), ({}, None), ({"cyber": None}, None), ({"cyber": "daybreak_blue"}, None),
    ({"cyber": [42]}, None), ({"cyber": []}, False), ({"cyber": ["standard"]}, False),
    ({"cyber": ["daybreak_blue"]}, True), ({"cyber": ["daybreak_red"]}, False),
])
def test_catalog_uses_authoritative_cyber_list_and_preserves_unknown(accounts, metadata, expected):
    caps.record_model_catalog("alice-token", [{"slug": "gpt-6.1-sol", "available_access_programs": metadata}])
    result = caps.daybreak_snapshot("alice", ["gpt-6.1-sol"])
    assert result["daybreak_models"]["gpt-6.1-sol"]["supported"] is expected
    assert result["daybreak_revision"] > 0
    assert caps.daybreak_snapshot("bob", ["gpt-6.1-sol"])["daybreak_models"]["gpt-6.1-sol"]["supported"] is None


def test_red_specialist_requires_red_program(accounts):
    caps.record_model_catalog("alice-token", [
        {"slug": "gpt-5.6-cyber", "available_access_programs": {"cyber": ["daybreak_red"]}},
        {"slug": "gpt-daybreak-red-latest", "available_access_programs": {"cyber": ["daybreak_blue"]}},
    ])
    result = caps.daybreak_snapshot("alice", ["gpt-5.6-cyber", "gpt-daybreak-red-latest"])["daybreak_models"]
    assert result["gpt-5.6-cyber"]["supported"] is True
    assert result["gpt-daybreak-red-latest"]["supported"] is False


def test_denial_survives_restart_is_account_scoped_and_refresh_replaces_it(accounts):
    caps.record_daybreak_model_denial("alice", "gpt-6-astra", "Daybreak isn't available for this model.")
    caps._token_accounts.clear()
    caps._pending_catalogs.clear()
    first = caps.daybreak_snapshot("alice", ["gpt-6-astra", "other"])
    assert first["daybreak_models"]["gpt-6-astra"]["supported"] is False
    assert first["daybreak_models"]["other"]["supported"] is None
    assert caps.daybreak_snapshot("bob", ["gpt-6-astra"])["daybreak_models"]["gpt-6-astra"]["supported"] is None
    caps.bind_account_token("alice", "rotated-token")
    caps.record_model_catalog("rotated-token", [{"slug": "gpt-6-astra", "available_access_programs": {"cyber": ["daybreak_blue"]}}])
    fresh = caps.daybreak_snapshot("alice", ["gpt-6-astra"])
    assert fresh["daybreak_revision"] > first["daybreak_revision"]
    assert fresh["daybreak_models"]["gpt-6-astra"]["supported"] is True


@pytest.mark.parametrize("code", ["access_program_not_enabled", "invalid_access_program", "invalid_token"])
def test_account_auth_or_program_error_does_not_blacklist_model(accounts, code):
    caps.record_request_denial({"Authorization": "Bearer alice-token"}, "gpt-6-sol", (code, "Access denied"))
    assert caps.daybreak_snapshot("alice", ["gpt-6-sol"])["daybreak_models"]["gpt-6-sol"]["supported"] is None


def test_reconnected_account_ignores_stale_inflight_token_denial(accounts):
    caps.bind_account_token("alice", "new-account-token")
    caps.record_request_denial({"Authorization": "Bearer alice-token"}, "gpt-6-sol", ("unsupported_access_program", "Old account denied"))
    assert caps.daybreak_snapshot("alice", ["gpt-6-sol"])["daybreak_models"]["gpt-6-sol"]["supported"] is None
    caps.record_request_denial({"authorization": "Bearer new-account-token"}, "gpt-6-sol", ("unsupported_access_program", "Current account denied"))
    assert caps.daybreak_snapshot("alice", ["gpt-6-sol"])["daybreak_models"]["gpt-6-sol"]["reason"] == "Current account denied"


def test_initial_discovery_metadata_is_saved_after_account_creation(accounts, monkeypatch):
    monkeypatch.setattr(subscription.httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={"models": [
        {"slug": "hidden", "visibility": "hide"},
        {"slug": "gpt-6-sol", "priority": 1, "available_access_programs": {"cyber": ["daybreak_blue"]}},
    ]}))
    assert subscription.fetch_available_models("new-token") == ["gpt-6-sol"]
    caps.bind_account_token("alice", "new-token")
    assert caps.daybreak_snapshot("alice", ["gpt-6-sol"])["daybreak_models"]["gpt-6-sol"]["supported"] is True
    with accounts() as db:
        stored = db.query(ProviderAuthSession.model_capabilities).filter(ProviderAuthSession.id == "alice").scalar()
    assert "new-token" not in stored


def test_optional_metadata_write_failure_does_not_break_reconnect(monkeypatch):
    caps.record_model_catalog("new-account-token", [{"slug": "gpt-6-sol"}])
    monkeypatch.setattr(caps, "_save_catalog", lambda *args: (_ for _ in ()).throw(RuntimeError("database unavailable")))
    caps.bind_account_token("new-account", "new-account-token")
    caps.record_model_catalog("new-account-token", [{"slug": "gpt-6-sol"}])


@pytest.mark.parametrize("status,body", [(503, {}), (200, {"models": []}), (200, {"models": "bad"})])
def test_failed_or_empty_catalog_refresh_keeps_denial_and_revision(accounts, monkeypatch, status, body):
    caps.record_daybreak_model_denial("alice", "gpt-6.1-sol", "Model denied Daybreak")
    before = caps.daybreak_snapshot("alice", ["gpt-6.1-sol"])
    monkeypatch.setattr(subscription.httpx, "get", lambda *args, **kwargs: httpx.Response(status, json=body))
    assert subscription.fetch_available_models("alice-token") == []
    assert caps.daybreak_snapshot("alice", ["gpt-6.1-sol"]) == before


@pytest.mark.parametrize("presets,default,expected", [
    (None, None, {"levels": [], "default": None, "supported": None}),
    ([], "medium", {"levels": [], "default": None, "supported": False}),
    ([{"effort": "low"}, {"effort": "ultra"}, {"effort": "low"}], "low",
     {"levels": ["low", "ultra"], "default": "low", "supported": True}),
    ([{"effort": "future_level"}], "future_level",
     {"levels": ["future_level"], "default": "future_level", "supported": True}),
    ([{"effort": "high"}], "medium", {"levels": ["high"], "default": None, "supported": True}),
    ([{"effort": "high"}, 3], None, {"levels": [], "default": None, "supported": None}),
])
def test_reasoning_catalog_preserves_model_levels_defaults_and_unknown(accounts, presets, default, expected):
    caps.record_model_catalog("alice-token", [{"slug": "gpt-6-sol", "supported_reasoning_levels": presets,
                                               "default_reasoning_level": default}])
    assert caps.daybreak_snapshot("alice", ["gpt-6-sol"])["reasoning_models"]["gpt-6-sol"] == expected
    assert caps.daybreak_snapshot("bob", ["gpt-6-sol"])["reasoning_models"]["gpt-6-sol"]["supported"] is None


def test_reasoning_validation_is_account_scoped_and_survives_daybreak_denial(accounts):
    caps.record_model_catalog("alice-token", [{"slug": "gpt-6-sol", "supported_reasoning_levels": [{"effort": "ultra"}]}])
    caps.record_model_catalog("bob-token", [{"slug": "gpt-6-sol", "supported_reasoning_levels": [{"effort": "low"}]}])
    caps.record_daybreak_model_denial("alice", "gpt-6-sol", "Daybreak model denial")
    assert caps.validate_reasoning_effort(" Ultra ", "gpt-6-sol", account_id="alice") == "ultra"
    assert caps.validate_reasoning_effort("ultra", "gpt-6-sol", headers={"Authorization": "Bearer alice-token"}) == "ultra"
    with pytest.raises(ValueError, match="not supported"):
        caps.validate_reasoning_effort("ultra", "gpt-6-sol", headers={"Authorization": "Bearer bob-token"})
    assert caps.validate_reasoning_effort("", "gpt-6-sol", account_id="bob") is None
    caps._token_accounts.clear()
    assert caps.validate_reasoning_effort("ultra", "gpt-6-sol", account_id="alice") == "ultra"
    assert caps.daybreak_snapshot("alice", ["gpt-6-sol"])["daybreak_models"]["gpt-6-sol"]["supported"] is False


@pytest.mark.parametrize("value", [1, True, [], {}, "high\nlow", "x" * 33])
def test_reasoning_normalizer_rejects_non_wire_values(value):
    with pytest.raises(ValueError):
        subscription.normalize_reasoning_effort(value)


@pytest.mark.asyncio
async def test_known_incompatible_effort_is_rejected_before_http_on_every_path(accounts, monkeypatch):
    from fastapi import HTTPException
    from src import llm_core
    caps.record_model_catalog("alice-token", [{"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "high"}]}])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: pytest.fail("Invalid effort reached HTTP"))
    monkeypatch.setattr(llm_core.httpx, "stream", lambda *args, **kwargs: pytest.fail("Invalid effort reached HTTP"))
    headers = {"Authorization": "Bearer alice-token"}
    candidates = [(subscription.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL, "gpt-6-luna", headers),
                  (subscription.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL, "different-model", headers)]
    messages = [{"role": "user", "content": "mock-only request"}]
    with pytest.raises(HTTPException, match="not supported"):
        llm_core.llm_call_with_fallback(candidates, messages, reasoning_effort="ultra")
    with pytest.raises(HTTPException, match="not supported"):
        await llm_core.llm_call_async_with_fallback(candidates, messages, reasoning_effort="ultra")
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback(candidates, messages, reasoning_effort="ultra")]
    assert len(chunks) == 1
    assert "fallback_forbidden" in chunks[0]
    assert "not supported" in chunks[0]
