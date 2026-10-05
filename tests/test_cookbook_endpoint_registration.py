from pathlib import Path
import inspect
import json

import pytest


ROOT = Path(__file__).resolve().parents[1]
COOKBOOK_RUNNING = ROOT / "static" / "js" / "cookbookRunning.js"


def _source() -> str:
    return COOKBOOK_RUNNING.read_text(encoding="utf-8")


def test_cookbook_marks_local_endpoint_registration_as_container_local():
    src = _source()
    assert "function _appendCookbookEndpointScope" in src
    assert "fd.append('container_local', 'true')" in src
    assert src.count("_appendCookbookEndpointScope(fd,") >= 3


def test_cookbook_does_not_use_local_as_endpoint_hostname():
    src = _source()
    assert "function _connectHostFromRemote" in src
    assert "if (!host || host === 'local') return fallback;" in src
    assert "const rawHost = task.remoteHost || 'localhost';" not in src


def test_cookbook_advertised_bind_urls_keep_connectable_host():
    src = _source()
    assert "function _endpointFromAdvertisedUrl" in src
    assert "_isAnyBindHost(u.hostname) ? currentHost" in src
    assert "host = u.hostname || host;" not in src


@pytest.fixture
def registration_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    from routes.cookbook_routes import setup_cookbook_routes
    engine = create_engine("sqlite:///:memory:")
    database.ModelEndpoint.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    serve = next(r.endpoint for r in setup_cookbook_routes().routes if r.path == "/api/model/serve")
    register = inspect.getclosurevars(serve).nonlocals["_auto_register_llm_endpoint"]
    yield register, factory, database.ModelEndpoint
    engine.dispose()


def test_llama_registration_replaces_fallback_with_discovery_without_extra_picker_row(registration_db, monkeypatch):
    import routes.model_routes as models
    from routes.cookbook_helpers import ServeRequest
    register, factory, endpoint = registration_db
    repo, actual = "Example/Model-GGUF", "/cache/model-Q8.gguf"
    monkeypatch.setattr(models, "_probe_endpoint", lambda *args, **kwargs: [actual])
    ep_id = register(ServeRequest(repo_id=repo, cmd="llama-server --model /cache/model-Q8.gguf --port 8001"), None)
    with factory() as db:
        ep = db.get(endpoint, ep_id)
        assert models._picker_models_for_endpoint(ep, ep.base_url, "local")[0] == [actual]
        assert models._cookbook_model_aliases(ep) == {repo: actual}


def test_relaunch_replaces_prior_launch_fallback_but_preserves_manual_pins(registration_db, monkeypatch):
    import routes.model_routes as models
    from routes.cookbook_helpers import ServeRequest
    register, factory, endpoint = registration_db
    monkeypatch.setattr(models, "_probe_endpoint", lambda *args, **kwargs: [])
    old = register(ServeRequest(repo_id="Example/Old-GGUF", cmd="llama-server --port 8001"), None)
    with factory() as db:
        ep = db.get(endpoint, old)
        ep.pinned_models = json.dumps(["Example/Old-GGUF", "manual-alias"])
        db.commit()
    new = register(ServeRequest(repo_id="Example/New-GGUF", cmd="llama-server --port 8001"), None)
    assert new == old
    with factory() as db:
        ep = db.get(endpoint, new)
        assert json.loads(ep.cached_models) == ["Example/New-GGUF"]
        assert json.loads(ep.pinned_models) == ["manual-alias", "Example/New-GGUF"]
