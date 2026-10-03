"""Manual index checks distinguish artifact availability from runtime readiness."""

import io
import json
import urllib.error

import pytest
from packaging.tags import Tag

import src.dependency_index as index


LINUX_314 = [Tag("cp314", "cp314", "manylinux_2_34_x86_64"), Tag("py3", "none", "any")]
MAC_314 = [Tag("cp314", "cp314", "macosx_14_0_arm64"), Tag("py3", "none", "any")]


def artifact(filename, *, kind="bdist_wheel", requires_python=">=3.10", yanked=False):
    return {"filename": filename, "packagetype": kind, "requires_python": requires_python, "yanked": yanked}


@pytest.fixture(autouse=True)
def clear_cache():
    index._cache.clear()
    yield
    index._cache.clear()


@pytest.fixture
def pypi(monkeypatch):
    state = {
        "calls": [],
        "payload": {
            "info": {"name": "demo", "version": "1.0", "requires_python": ">=3.10"},
            "urls": [artifact("demo-1.0-py3-none-any.whl")],
        },
    }

    def urlopen(request, timeout):
        assert request.full_url.startswith("https://pypi.org/pypi/")
        assert timeout == 5
        state["calls"].append(request.full_url)
        payload = state["payload"]
        if isinstance(payload, Exception):
            raise payload
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(index.urllib.request, "urlopen", urlopen)
    return state


def check(name="demo", tags=LINUX_314, version="3.14.7"):
    return index.check_latest_release(name, python_version=version, supported_tags=tags)


def test_latest_sglang_style_release_without_314_artifact_is_actionable(pypi):
    pypi["payload"]["urls"] = [
        artifact(f"demo-1.0-cp{version}-cp{version}-manylinux_2_34_x86_64.whl")
        for version in [310, 311, 312, 313]
    ]
    result = check()
    assert result["latest_version"] == "1.0"
    assert result["has_compatible_artifact"] is False
    assert result["install_supported"] is False
    assert "no compatible wheel or source archive" in result["compatibility_note"]
    assert "container" in result["compatibility_note"]


def test_remote_tags_are_used_instead_of_local_machine(pypi):
    pypi["payload"]["urls"] = [artifact("demo-1.0-cp314-cp314-manylinux_2_34_x86_64.whl")]
    assert check(tags=LINUX_314)["artifact_kind"] == "wheel"
    assert check(tags=MAC_314)["has_compatible_artifact"] is False
    assert len(pypi["calls"]) == 1  # Metadata is reusable; platform decisions are not cached.


def test_supplied_tag_strings_include_abi3_and_compressed_tags(pypi):
    pypi["payload"]["urls"] = [artifact("demo-1.0-cp38-abi3-manylinux_2_28_x86_64.whl")]
    result = check(tags=["cp38-abi3-manylinux_2_28_x86_64.manylinux_2_34_x86_64"])
    assert result["has_compatible_artifact"] is True


def test_local_default_uses_actual_interpreter_tags(pypi, monkeypatch):
    monkeypatch.setattr(index, "sys_tags", lambda: iter(MAC_314))
    pypi["payload"]["urls"] = [artifact("demo-1.0-cp314-cp314-macosx_14_0_arm64.whl")]
    assert index.check_latest_release("demo")["artifact_kind"] == "wheel"


def test_source_only_release_is_buildable_not_unavailable(pypi):
    pypi["payload"]["urls"] = [artifact("demo-1.0.tar.gz", kind="sdist")]
    result = check()
    assert result["install_supported"] is True
    assert result["artifact_kind"] == "source"
    assert "building may require" in result["compatibility_note"]


def test_python_requirement_wins_over_universal_wheel(pypi):
    pypi["payload"]["info"]["requires_python"] = "<3.14,>=3.10"
    result = check()
    assert result["has_compatible_artifact"] is False
    assert "requires Python <3.14,>=3.10" in result["compatibility_note"]


def test_yanked_or_file_specific_python_incompatible_artifacts_do_not_pass(pypi):
    pypi["payload"]["urls"] = [
        artifact("demo-1.0-py3-none-any.whl", yanked=True),
        artifact("demo-1.0.tar.gz", kind="sdist", requires_python="<3.14"),
    ]
    assert check()["has_compatible_artifact"] is False


def test_wheel_of_different_distribution_or_release_cannot_pass(pypi):
    pypi["payload"]["urls"] = [
        artifact("another-1.0-py3-none-any.whl"),
        artifact("demo-0.9-py3-none-any.whl"),
    ]
    assert check()["has_compatible_artifact"] is False


def test_cache_expires_and_new_upstream_wheel_becomes_available(pypi, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(index.time, "monotonic", lambda: now[0])
    pypi["payload"]["urls"] = [artifact("demo-1.0-cp313-cp313-manylinux_2_34_x86_64.whl")]
    assert check()["install_supported"] is False
    pypi["payload"]["urls"] = [artifact("demo-1.0-py3-none-any.whl")]
    assert check()["install_supported"] is False
    now[0] += index.CACHE_TTL_SECONDS + 1
    assert check()["install_supported"] is True
    assert len(pypi["calls"]) == 2


@pytest.mark.parametrize("failure", [TimeoutError(), urllib.error.URLError("offline"), ValueError("malformed")])
def test_failures_remain_unknown_and_retryable(pypi, failure):
    pypi["payload"] = failure
    result = check()
    assert result["install_supported"] is None
    assert result["has_compatible_artifact"] is None
    assert "Retry Check dependencies" in result["compatibility_note"]
    check()
    assert len(pypi["calls"]) == 2


def test_oversized_metadata_is_unknown_not_a_false_compatibility_block(pypi, monkeypatch):
    monkeypatch.setattr(index, "MAX_RESPONSE_BYTES", 3)
    assert check()["install_supported"] is None


@pytest.mark.parametrize("name", ["../demo", "https://example.com", "demo[extra]", "demo>=1", "", "x" * 129])
def test_only_distribution_names_can_reach_official_index(pypi, name):
    assert check(name)["install_supported"] is None
    assert not pypi["calls"]


def test_partial_remote_environment_does_not_fall_back_to_local_tags(pypi):
    assert index.check_latest_release("demo", python_version="3.14.7")["install_supported"] is None
    assert index.check_latest_release("demo", supported_tags=LINUX_314)["install_supported"] is None
    assert not pypi["calls"]


def test_cache_size_is_bounded(pypi, monkeypatch):
    monkeypatch.setattr(index, "MAX_CACHE_ENTRIES", 2)
    for name in ["first", "second", "third"]:
        pypi["payload"]["info"]["name"] = name
        pypi["payload"]["urls"] = [artifact(f"{name}-1.0-py3-none-any.whl")]
        assert check(name)["install_supported"] is True
    assert list(index._cache) == ["second", "third"]


def test_normalized_name_and_release_identity_are_verified(pypi):
    pypi["payload"]["info"]["name"] = "My_Package"
    pypi["payload"]["urls"] = [artifact("my_package-1.0-py3-none-any.whl")]
    assert check("My.Package")["install_supported"] is True
    assert pypi["calls"] == ["https://pypi.org/pypi/my-package/json"]
    assert check("different")["install_supported"] is None
