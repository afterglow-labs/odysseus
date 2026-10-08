"""CPU checks for the H3 worker's PyTorch allocator environment override."""
import importlib.util
import logging
import os
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("h3_allocator_worker", Path(__file__).parents[1] / "scripts/h3_video_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


@pytest.mark.parametrize("environ", [
    {},
    {"PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128"},
    {"PYTORCH_ALLOC_CONF": "backend:native"},
    {"PYTORCH_ALLOC_CONF": "backend:native", "PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128"},
    {"ODYSSEUS_H3_PYTORCH_ALLOC_CONF": "", "PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128"},
])
def test_no_app_override_preserves_allocator_environment(environ):
    original = environ.copy()
    worker.configure_cuda_allocator(environ)
    assert environ == original


def test_app_override_sets_modern_variable_and_preserves_legacy(caplog):
    configured = "backend:native,expandable_segments:True"
    environ = {
        "ODYSSEUS_H3_PYTORCH_ALLOC_CONF": configured,
        "PYTORCH_ALLOC_CONF": "expandable_segments:False",
        "PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128",
    }
    with caplog.at_level(logging.INFO):
        worker.configure_cuda_allocator(environ)

    assert environ["PYTORCH_ALLOC_CONF"] == configured
    assert environ["PYTORCH_CUDA_ALLOC_CONF"] == "max_split_size_mb:128"
    assert environ["ODYSSEUS_H3_PYTORCH_ALLOC_CONF"] == configured
    assert any(record.levelno == logging.INFO and configured in record.getMessage()
               for record in caplog.records)


def test_explicit_environment_does_not_mutate_process_environment(monkeypatch):
    monkeypatch.setenv("PYTORCH_ALLOC_CONF", "expandable_segments:False")
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:64")
    monkeypatch.setenv("ODYSSEUS_H3_PYTORCH_ALLOC_CONF", "backend:cudaMallocAsync")
    original = dict(os.environ)
    isolated = {"ODYSSEUS_H3_PYTORCH_ALLOC_CONF": "expandable_segments:True"}

    worker.configure_cuda_allocator(isolated)

    assert isolated["PYTORCH_ALLOC_CONF"] == "expandable_segments:True"
    assert "PYTORCH_CUDA_ALLOC_CONF" not in isolated
    assert dict(os.environ) == original


def test_default_environment_updates_the_worker_process(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_H3_PYTORCH_ALLOC_CONF", "expandable_segments:True")
    monkeypatch.setenv("PYTORCH_ALLOC_CONF", "expandable_segments:False")
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:128")

    worker.configure_cuda_allocator()

    assert os.environ["PYTORCH_ALLOC_CONF"] == "expandable_segments:True"
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "max_split_size_mb:128"
