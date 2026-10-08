"""CPU checks for explicit H3 fast-storage roots and native fallback."""
import importlib.util
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SPEC = importlib.util.spec_from_file_location("h3_fast_storage_worker", Path(__file__).parents[1] / "scripts/h3_video_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_H3_FAST_DISK_ROOTS", raising=False)
    native = Mock(return_value=None)
    return SimpleNamespace(fast_storage=native), native


@pytest.fixture
def models(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    local = root / "model.safetensors"
    local.touch()
    external = tmp_path / "external.safetensors"
    external.touch()
    return root, local, external


def test_configured_root_overrides_only_local_regular_files(storage, models):
    module, native = storage
    root, local, external = models
    worker.configure_fast_storage(module, roots=[root])

    assert module.fast_storage(local) is True
    native.assert_not_called()
    for path in (external, root, root / "missing.safetensors"):
        assert module.fast_storage(path) is None
        native.assert_called_with(path)


@pytest.mark.parametrize("result", [True, False, None])
def test_unconfigured_files_keep_native_result(storage, models, result):
    module, native = storage
    root, _, external = models
    native.return_value = result
    worker.configure_fast_storage(module, roots=[root])

    assert module.fast_storage(external) is result
    native.assert_called_once_with(external)


def test_similar_directory_prefix_does_not_match(storage, models):
    module, native = storage
    root, _, _ = models
    sibling = root.with_name("models-old")
    sibling.mkdir()
    model = sibling / "model.safetensors"
    model.touch()
    worker.configure_fast_storage(module, roots=[root])

    assert module.fast_storage(model) is None
    native.assert_called_once_with(model)


def test_symlinks_follow_the_target_storage_location(storage, models):
    module, native = storage
    root, local, external = models
    outbound = root / "external-link.safetensors"
    outbound.symlink_to(external)
    inbound = external.with_name("local-link.safetensors")
    inbound.symlink_to(local)
    worker.configure_fast_storage(module, roots=[root])

    assert module.fast_storage(outbound) is None
    native.assert_called_once_with(outbound)
    native.reset_mock()
    assert module.fast_storage(inbound) is True
    native.assert_not_called()


def test_nested_mount_keeps_native_detection(storage, models, monkeypatch):
    module, native = storage
    root, _, _ = models
    mount = root / "mounted"
    mount.mkdir()
    model = mount / "model.safetensors"
    model.touch()
    original_stat = os.stat
    different_device = root.stat().st_dev + 1

    def stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and os.path.abspath(path) == str(model):
            fields = list(result)
            fields[2] = different_device
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(os, "stat", stat)
    worker.configure_fast_storage(module, roots=[root])

    assert module.fast_storage(model) is None
    native.assert_called_once_with(model)


def test_environment_configures_roots(storage, models, monkeypatch):
    module, native = storage
    root, local, _ = models
    monkeypatch.setenv("ODYSSEUS_H3_FAST_DISK_ROOTS", json.dumps([str(root)]))
    worker.configure_fast_storage(module)

    assert module.fast_storage(local) is True
    native.assert_not_called()


def test_no_configuration_keeps_native_detection(storage, models):
    module, native = storage
    _, local, _ = models
    worker.configure_fast_storage(module)

    assert module.fast_storage(local) is None
    native.assert_called_once_with(local)


@pytest.mark.parametrize("value", ["not-json", '"/models"', "{}", "null", "[null]", "[123]"])
def test_invalid_environment_warns_and_keeps_native_detection(storage, models, monkeypatch, caplog, value):
    module, native = storage
    _, local, _ = models
    monkeypatch.setenv("ODYSSEUS_H3_FAST_DISK_ROOTS", value)
    with caplog.at_level(logging.WARNING):
        worker.configure_fast_storage(module)

    assert module.fast_storage(local) is None
    native.assert_called_once_with(local)
    assert caplog.records


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_invalid_root_warns_and_keeps_native_detection(storage, models, caplog, root_kind):
    module, native = storage
    root, local, _ = models
    configured = root / "missing" if root_kind == "missing" else local
    with caplog.at_level(logging.WARNING):
        worker.configure_fast_storage(module, roots=[configured])

    assert module.fast_storage(local) is None
    native.assert_called_once_with(local)
    assert caplog.records


def test_repeated_configuration_does_not_stack_wrappers(storage, models):
    module, native = storage
    root, local, external = models
    worker.configure_fast_storage(module, roots=[root])
    worker.configure_fast_storage(module, roots=[root])

    assert module.fast_storage(local) is True
    assert module.fast_storage(external) is None
    native.assert_called_once_with(external)
    native.reset_mock()
    worker.configure_fast_storage(module, roots=[])
    assert module.fast_storage(local) is None
    native.assert_called_once_with(local)
