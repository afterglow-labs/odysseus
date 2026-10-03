from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from routes.tts_routes import setup_tts_routes
from services.tts import tts_service as module


def _settings():
    return {"tts_provider": "local", "tts_model": "kokoro", "tts_voice": "af_heart"}


def test_atomic_publication_serializes_concurrent_read_clear_and_stats(tmp_path, monkeypatch):
    service = module.TTSService(cache_dir=tmp_path)
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "tts_provider": "disabled", "tts_model": "", "tts_voice": "",
    })
    old, new = b"old-complete-audio", b"new-complete-audio" * 1000
    service._put_cache("voice", old)
    at_publish = threading.Event()
    publish = threading.Event()
    attempts = {name: threading.Event() for name in ("read", "clear", "stats")}
    actual_lock = service._cache_lock
    local = threading.local()

    class ObservedLock:
        def __enter__(self):
            name = getattr(local, "operation", None)
            if name in attempts:
                attempts[name].set()
            return actual_lock.__enter__()

        def __exit__(self, *args):
            return actual_lock.__exit__(*args)

    service._cache_lock = ObservedLock()
    actual_replace = os.replace

    def paused_replace(source, target):
        # The entire new payload exists only in the temporary file. An
        # independent reader still sees the complete previous publication.
        assert Path(source).read_bytes() == new
        assert Path(target).read_bytes() == old
        at_publish.set()
        assert publish.wait(timeout=3)
        actual_replace(source, target)

    monkeypatch.setattr(module.os, "replace", paused_replace)

    def operation(name, callback):
        local.operation = name
        return callback()

    with ThreadPoolExecutor(max_workers=4) as pool:
        writer = pool.submit(service._put_cache, "voice", new)
        try:
            assert at_publish.wait(timeout=2)
            reader = pool.submit(operation, "read", lambda: service._get_cached("voice"))
            clearer = pool.submit(operation, "clear", service.clear_cache)
            stats = pool.submit(operation, "stats", service.get_stats)
            # Each operation has reached the shared lock while publication is
            # paused, eliminating scheduling races in the blocked assertions.
            assert all(event.wait(timeout=2) for event in attempts.values())
            assert not reader.done() and not clearer.done() and not stats.done()
        finally:
            publish.set()
        writer.result(timeout=2)
        assert reader.result(timeout=2) in (new, None)
        clearer.result(timeout=2)
        assert stats.result(timeout=2)["cache_entries"] in (0, 1)
    assert service._get_cached("voice") is None
    assert list(tmp_path.iterdir()) == []


def test_failed_cache_publish_preserves_previous_complete_audio(tmp_path, monkeypatch):
    service = module.TTSService(cache_dir=tmp_path)
    service._put_cache("voice", b"original")

    def fail_replace(*args):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    service._put_cache("voice", b"replacement")
    assert service._get_cached("voice") == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["voice.wav"]


@pytest.mark.asyncio
async def test_explicit_synthesis_retries_failed_initialization_without_poll_retries(tmp_path, monkeypatch):
    service = module.TTSService(cache_dir=tmp_path)
    monkeypatch.setattr(service, "_load_settings", _settings)
    monkeypatch.setattr(module.kokoro_runtime, "is_installed", lambda: True)
    monkeypatch.setattr(module.kokoro_runtime, "is_model_cached", lambda: False)
    attempts = []

    def initialize():
        attempts.append(True)
        if len(attempts) == 1:
            return SimpleNamespace(available=False, error="download timed out")
        return SimpleNamespace(available=True, error=None, synthesize_raw=lambda *args, **kwargs: b"RIFFcomplete")

    monkeypatch.setattr(module, "_KokoroPipeline", initialize)
    app = FastAPI()
    app.include_router(setup_tts_routes(service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/tts/synthesize", json={"text": "hello"})).status_code == 500
        for _ in range(2):
            stats = (await client.get("/api/tts/stats")).json()
            assert stats["available"] and stats["ready"]
            assert not stats["model_loaded"]
            assert stats["error"] == "download timed out"
        assert len(attempts) == 1
        response = await client.post("/api/tts/synthesize", json={"text": "hello"})
        assert response.status_code == 200 and response.content == b"RIFFcomplete"
        assert len(attempts) == 2
        assert "error" not in (await client.get("/api/tts/stats")).json()
