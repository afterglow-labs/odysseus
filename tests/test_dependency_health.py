"""Dependency metadata checks must be precise without importing optional tools."""

import builtins
import importlib
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

import src.dependency_health as dependency_health
from src.dependency_health import check_dependency_health


def _check(requirements, versions, dependencies=None, **kwargs):
    def version(name):
        if name not in versions:
            raise PackageNotFoundError(name)
        return versions[name]

    return check_dependency_health(
        requirements, version_fn=version,
        requires_fn=lambda name: (dependencies or {}).get(name, []), **kwargs,
    )


def test_missing_transitive_dependency_keeps_compatible_repair_specifier():
    result = _check(
        ["realesrgan"], {"realesrgan": "0.3.0", "basicsr": "1.4.2"},
        {"realesrgan": ["basicsr>=1.4.2"], "basicsr": ["numpy>=1.21,<3"]},
    )

    assert result["installed"] is False
    assert result["conflicts"] == []
    assert result["versions"] == {"basicsr": "1.4.2", "realesrgan": "0.3.0"}
    issue = result["missing"][0]
    assert issue["name"] == "numpy"
    assert issue["requirement"] == "numpy<3,>=1.21"
    assert issue["kind"] == "missing"
    assert issue["required_by"] == ["basicsr"]
    assert result["issues"] == result["missing"]


def test_incompatible_dependency_combines_all_active_parent_constraints():
    result = _check(
        ["first", "second"], {"first": "1", "second": "1", "shared": "1.5"},
        {"first": ["shared>=2"], "second": ["shared<3"]},
    )

    assert result["missing"] == []
    issue = result["conflicts"][0]
    assert issue["requirement"] == "shared<3,>=2"
    assert issue["installed_version"] == "1.5"
    assert issue["required_by"] == ["first", "second"]
    assert issue["kind"] == "incompatible"


@pytest.mark.parametrize("extra,expected", [("cpu", "onnxruntime"), ("gpu", "onnxruntime-gpu")])
def test_only_requested_extras_and_current_environment_are_followed(extra, expected):
    result = _check(
        [f"rembg[{extra}]"], {"rembg": "2.0.0"},
        {"rembg": [
            'onnxruntime>=1; extra == "cpu"',
            'onnxruntime-gpu>=1; extra == "gpu"',
            'pytest; extra == "dev"',
            'linux-only; sys_platform == "linux"',
            'old-python; python_version < "3.10"',
        ]},
        environment={"sys_platform": "darwin", "python_version": "3.14"},
    )

    assert [issue["name"] for issue in result["missing"]] == [expected]
    assert result["missing"][0]["requirement"] == f"{expected}>=1"


def test_extra_contexts_expand_across_diamond_graph_without_cycle_loop():
    result = _check(
        ["FIRST", "second"],
        {"first": "1", "second": "1", "shared-dep": "1", "core": "1", "x-tool": "1"},
        {
            "first": ["Shared_Dep[x]"],
            "second": ["shared-dep[y]"],
            "shared-dep": ["core", 'x-tool; extra == "x"', 'y-tool>=2; extra == "y"'],
            "core": ["first"],
        },
    )

    assert [issue["name"] for issue in result["missing"]] == ["y-tool"]
    assert "shared-dep" in result["versions"]


def test_inactive_root_marker_does_not_probe_missing_distribution():
    def unexpected_lookup(name):
        pytest.fail(f"inactive requirement was inspected: {name}")

    result = check_dependency_health(
        'windows-tool; sys_platform == "win32"', version_fn=unexpected_lookup,
        environment={"sys_platform": "darwin"},
    )
    assert result["installed"] is True
    assert result["versions"] == {}


def test_vcs_requirement_uses_explicit_distribution_alias():
    spec = "git+https://example.test/vendor/package.git@fixed-commit"
    result = _check(
        [spec], {"real-dist": "2.1"}, {"real-dist": ["child>=3"]},
        aliases={spec: "real-dist>=2"},
    )
    assert result["versions"] == {"real-dist": "2.1"}
    assert result["missing"][0]["requirement"] == "child>=3"


def test_satisfied_prerelease_and_requested_extras_are_healthy():
    result = _check(
        ["tool[vision]>=2"], {"tool": "2.1rc1", "image": "1"},
        {"tool": ['image; extra == "vision"', 'test-runner; extra == "test"']},
    )
    assert result["installed"] is True
    assert result["issues"] == []


@pytest.mark.parametrize("installed_version", ["0.5", "invalid-version"])
def test_direct_requirement_failure_is_reported(installed_version):
    result = _check("tool>=1", {"tool": installed_version})
    issue = result["conflicts"][0]
    assert issue["requirement"] == "tool>=1"
    assert issue["required_by"] == []
    assert issue["installed_version"] == installed_version


def test_unreadable_and_malformed_metadata_do_not_claim_readiness():
    def broken_metadata(name):
        raise OSError("metadata is unreadable")

    result = check_dependency_health("tool", version_fn=lambda name: "1", requires_fn=broken_metadata)
    assert result["installed"] is False
    assert "Cannot read required dependencies" in result["conflicts"][0]["message"]

    malformed = _check("tool>=1", {"tool": "1"}, {"tool": ["broken requirement???"]})
    assert malformed["installed"] is False
    assert "Invalid dependency metadata" in malformed["conflicts"][0]["message"]
    assert malformed["conflicts"][0]["requirement"] == "tool>=1"


@pytest.mark.parametrize("vendored_available", [True, False])
def test_standalone_source_handles_missing_packaging(monkeypatch, vendored_available):
    original_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name.startswith("packaging"):
            raise ImportError("standalone packaging absent")
        if name.startswith("pip._vendor.packaging"):
            if not vendored_available:
                raise ImportError("pip absent")
            return importlib.import_module(name.removeprefix("pip._vendor."))
        return original_import(name, *args, **kwargs)

    source = Path(dependency_health.__file__).read_text(encoding="utf-8")
    namespace = {"__name__": "remote_dependency_health"}
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", isolated_import)
        exec(compile(source, "remote_dependency_health.py", "exec"), namespace)
    result = namespace["check_dependency_health"](
        "tool", version_fn=lambda name: "1", requires_fn=lambda name: [],
    )

    assert result["installed"] is vendored_available
    if not vendored_available:
        assert result["missing"][0]["requirement"] == "packaging"
