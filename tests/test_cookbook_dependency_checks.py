"""Package status stays offline; explicit audits use the selected environment."""

from copy import deepcopy
import importlib.metadata
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import routes.shell_routes as routes


@pytest.fixture
def package_route(monkeypatch):
    state = {
        "packages": [
            {"name": "parent", "pip": "parent", "target": "local", "install_supported": True},
            {"name": "healthy", "pip": "healthy", "target": "local", "install_supported": True},
        ],
        "versions": {"parent": "1", "healthy": "1"},
        "requires": {},
    }

    def version(name):
        if name not in state["versions"]:
            raise importlib.metadata.PackageNotFoundError(name)
        return state["versions"][name]

    monkeypatch.setattr(routes, "dependency_catalog", lambda **kwargs: deepcopy(state["packages"]))
    monkeypatch.setattr(routes, "_require_admin", lambda request: None)
    monkeypatch.setattr(routes, "_reject_cross_site", lambda request: None)
    monkeypatch.setattr(routes, "_prepend_user_install_bins_to_path", lambda: None)
    monkeypatch.setattr(routes, "_import_optional_dependency_for_status", lambda name: SimpleNamespace())
    monkeypatch.setattr(importlib.metadata, "version", version)
    monkeypatch.setattr(importlib.metadata, "requires", lambda name: state["requires"].get(name, []))
    monkeypatch.setattr("site.getusersitepackages", lambda: "")
    monkeypatch.setattr(routes.shutil, "which", lambda name: None)

    def no_network(*args, **kwargs):
        pytest.fail("automatic package checks must not contact package indexes")

    monkeypatch.setattr(routes, "check_latest_release", no_network)
    endpoint = next(route.endpoint for route in routes.setup_shell_routes().routes if route.path == "/api/cookbook/packages")

    async def get(**kwargs):
        result = await endpoint(SimpleNamespace(), **kwargs)
        return {package["name"]: package for package in result["packages"]}

    return state, get


@pytest.mark.asyncio
@pytest.mark.parametrize("child_version,kind", [(None, "missing"), ("1", "incompatible")])
async def test_transitive_metadata_damage_marks_only_affected_feature_for_repair(package_route, child_version, kind):
    state, get = package_route
    state["requires"] = {"parent": ["child>=2,<3"]}
    if child_version:
        state["versions"]["child"] = child_version

    rows = await get()

    assert rows["healthy"]["installed"] is True
    assert rows["parent"]["installed"] is False
    assert rows["parent"]["needs_repair"] is True
    issue = rows["parent"]["dependency_issues"][0]
    assert issue["name"] == "child"
    assert issue["kind"] == kind
    assert issue["requirement"] == "child<3,>=2"
    assert "index_checks" not in rows["parent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("broken native extension"), SystemExit("runtime unavailable")])
async def test_runtime_failure_does_not_hide_other_packages(package_route, monkeypatch, error):
    _, get = package_route

    def runtime_import(name):
        if name == "parent":
            raise error
        return SimpleNamespace()

    monkeypatch.setattr(routes, "_import_optional_dependency_for_status", runtime_import)
    rows = await get()
    assert rows["parent"]["installed"] is False
    assert rows["parent"]["needs_repair"] is True
    assert str(error) in rows["parent"]["probe_error"]
    assert rows["healthy"]["installed"] is True


@pytest.mark.asyncio
async def test_unexpected_health_probe_failure_is_isolated(package_route, monkeypatch):
    _, get = package_route
    original = routes.check_dependency_health

    def health(specs):
        if specs == ["parent"]:
            raise RuntimeError("unreadable metadata")
        return original(specs)

    monkeypatch.setattr(routes, "check_dependency_health", health)
    rows = await get()
    assert rows["parent"]["installed"] is None
    assert "unreadable metadata" in rows["parent"]["probe_error"]
    assert rows["healthy"]["installed"] is True


@pytest.mark.asyncio
async def test_explicit_index_failure_preserves_platform_restrictions(package_route, monkeypatch):
    state, get = package_route
    state["packages"][0].update(install_supported=False, install_hint="Linux server required.")
    calls = []

    def check(name, **kwargs):
        calls.append((name, kwargs))
        return {"latest_version": "2", "install_supported": name == "parent", "compatibility_note": f"{name} artifact result"}

    monkeypatch.setattr(routes, "check_latest_release", check)
    rows = await get(check_index=True)
    assert sorted(calls) == [("healthy", {}), ("parent", {})]
    assert rows["parent"]["install_supported"] is False
    assert "Linux server required" in rows["parent"]["compatibility_note"]
    assert rows["healthy"]["install_supported"] is False
    assert rows["healthy"]["latest_version"] == "2"
    # Published-artifact availability does not change current runtime readiness.
    assert rows["healthy"]["installed"] is True


@pytest.mark.asyncio
async def test_unknown_index_result_keeps_install_choices(package_route, monkeypatch):
    _, get = package_route

    def unavailable(name, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(routes, "check_latest_release", unavailable)
    rows = await get(check_index=True)
    assert all(row["install_supported"] is True for row in rows.values())
    assert all("Could not verify" in row["compatibility_note"] for row in rows.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", [None, {"python_version": "3.12.9"}, {"python_version": "3.12.9", "supported_tags": ["cp312-cp312-manylinux_2_34_x86_64"]}])
async def test_remote_index_uses_only_remote_python_and_tags(package_route, monkeypatch, environment):
    state, get = package_route
    state["packages"][0]["target"] = "remote"
    payload = {
        "parent": {"dists": {"parent": "1"}, "health": {"installed": True, "versions": {"parent": "1"}, "issues": []}},
    }
    if environment is not None:
        payload["_environment"] = environment
    process = SimpleNamespace(communicate=AsyncMock(return_value=(json.dumps(payload).encode(), b"")), returncode=0)
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    calls = []

    def check(name, **kwargs):
        calls.append((name, kwargs))
        return {"latest_version": "2", "install_supported": True, "compatibility_note": "Matching wheel."}

    monkeypatch.setattr(routes, "check_latest_release", check)
    rows = await get(host="tester@gpu-box", check_index=True)
    assert ("healthy", {}) in calls  # Local app tools stay local on a remote tab.
    if environment and environment.get("supported_tags"):
        assert ("parent", environment) in calls
    else:
        assert not any(name == "parent" for name, kwargs in calls)
        assert "unknown" in rows["parent"]["compatibility_note"].lower()
        assert rows["parent"]["install_supported"] is True


def test_remote_probe_isolates_a_broken_package_and_reports_its_environment(package_route, monkeypatch, capsys):
    def find_spec(name):
        if name == "parent":
            raise RuntimeError("broken module metadata")
        return SimpleNamespace(loader=object(), origin="fixture", submodule_search_locations=[])

    monkeypatch.setattr("importlib.util.find_spec", find_spec)
    source = routes._package_probe_script(["parent", "healthy"], include_environment=True)
    exec(compile(source, "remote_dependency_probe.py", "exec"), {"__name__": "remote_probe"})
    result = json.loads(capsys.readouterr().out)
    assert "broken module metadata" in result["parent"]["error"]
    assert result["healthy"]["health"]["installed"] is True
    assert result["_environment"]["python_version"]
    assert result["_environment"]["supported_tags"]
