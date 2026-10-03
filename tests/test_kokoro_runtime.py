import hashlib
import io
import sys
import tarfile
import threading
from types import SimpleNamespace
import wave

import httpx
import numpy as np
import pytest

from services.tts import kokoro_runtime as runtime
from services.tts.tts_service import TTSService, _safe_speed


def _archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:bz2") as archive:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def _bundle():
    return _archive([(f"{runtime.MODEL_NAME}/{name}", b"data", tarfile.REGTYPE) for name in (
        "model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt",
        "espeak-ng-data/phontab", "LICENSE",
    )])


def _download(monkeypatch, payload):
    from contextlib import contextmanager

    monkeypatch.setattr(runtime, "MODEL_DOWNLOAD_BYTES", len(payload))
    monkeypatch.setattr(runtime, "MODEL_SHA256", hashlib.sha256(payload).hexdigest())
    calls = []

    @contextmanager
    def stream(*args, **kwargs):
        calls.append(args)
        yield httpx.Response(200, content=payload, request=httpx.Request("GET", runtime.MODEL_URL))

    monkeypatch.setattr(runtime.httpx, "stream", stream)
    return calls


def test_model_install_is_complete_and_cached_without_network(tmp_path, monkeypatch):
    calls = _download(monkeypatch, _bundle())
    target = runtime.ensure_model(tmp_path)
    assert runtime._model_ready(target)
    assert runtime.ensure_model(tmp_path) == target
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("failure", ("checksum", "oversized", "truncated"))
def test_bad_download_never_publishes_or_leaves_partial_model(tmp_path, monkeypatch, failure):
    payload = _bundle()
    _download(monkeypatch, payload)
    if failure == "checksum":
        monkeypatch.setattr(runtime, "MODEL_SHA256", "0" * 64)
    elif failure == "oversized":
        monkeypatch.setattr(runtime, "MODEL_DOWNLOAD_BYTES", len(payload) - 1)
    else:
        monkeypatch.setattr(runtime, "MODEL_DOWNLOAD_BYTES", len(payload) + 1)
    with pytest.raises(ValueError):
        runtime.ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name,kind", (
    ("../escaped", tarfile.REGTYPE),
    (f"{runtime.MODEL_NAME}/../../escaped", tarfile.REGTYPE),
    ("/escaped", tarfile.REGTYPE),
    (f"{runtime.MODEL_NAME}/link", tarfile.SYMTYPE),
    (f"{runtime.MODEL_NAME}/link", tarfile.LNKTYPE),
))
def test_archive_rejects_escape_and_links(tmp_path, monkeypatch, name, kind):
    _download(monkeypatch, _archive([(name, b"", kind)]))
    with pytest.raises(ValueError, match="Unsafe"):
        runtime.ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_extraction_size_is_bounded(tmp_path, monkeypatch):
    _download(monkeypatch, _bundle())
    monkeypatch.setattr(runtime, "MODEL_EXTRACT_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="extraction limit"):
        runtime.ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_pipeline_reports_download_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace())
    monkeypatch.setattr(runtime, "ensure_model", lambda _: (_ for _ in ()).throw(ValueError("checksum mismatch")))
    pipeline = runtime.KokoroPipeline(tmp_path)
    assert not pipeline.available
    assert "checksum mismatch" in pipeline.error


def _pipeline(samples):
    pipeline = object.__new__(runtime.KokoroPipeline)
    pipeline.available = True
    pipeline._synthesis_lock = threading.Lock()
    calls = []

    def generate(text, **kwargs):
        calls.append((text, kwargs))
        return SimpleNamespace(samples=samples, sample_rate=24000)

    pipeline.pipeline = SimpleNamespace(generate=generate)
    return pipeline, calls


def test_voice_mapping_speed_and_wav_pcm_clipping():
    pipeline, calls = _pipeline(np.array([-2, -1, 0, 1, 2], dtype=np.float32))
    audio = pipeline.synthesize_raw("Hello", "af_heart", speed=1.25)
    assert calls == [("Hello", {"sid": 3, "speed": 1.25})]
    with wave.open(io.BytesIO(audio)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert np.frombuffer(wav.readframes(5), dtype="<i2").tolist() == [-32767, -32767, 0, 32767, 32767]
    assert runtime.VOICE_IDS["bf_emma"] == 21
    assert runtime.VOICE_IDS["bm_lewis"] == 27


def test_unknown_voice_and_empty_text_do_not_run_model():
    pipeline, calls = _pipeline(np.ones(10))
    assert pipeline.synthesize_raw("Hello", "unknown") is None
    assert pipeline.synthesize_raw("Hello", ["af_heart"]) is None
    assert pipeline.synthesize_raw(" ", "af_heart") is None
    assert calls == []


def test_nonfinite_audio_is_rejected():
    pipeline, _ = _pipeline(np.array([float("nan")]))
    assert pipeline.synthesize_raw("Hello") is None


def test_service_passes_speed_to_local_provider(tmp_path, monkeypatch):
    service = TTSService(cache_dir=tmp_path)
    pipeline, calls = _pipeline(np.ones(10))
    monkeypatch.setattr(service, "_get_kokoro", lambda: pipeline)
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "tts_provider": "local", "tts_model": "kokoro", "tts_voice": "af_heart", "tts_speed": "1.5",
    })
    assert service.synthesize("Hello", use_cache=False)
    assert calls[0][1]["speed"] == 1.5


@pytest.mark.parametrize("value", ("nan", "inf", "-inf"))
def test_nonfinite_speed_uses_safe_default(value):
    assert _safe_speed(value) == 1.0


def test_capability_poll_does_not_load_or_download_model(tmp_path, monkeypatch):
    service = TTSService(cache_dir=tmp_path)
    monkeypatch.setattr(runtime, "is_installed", lambda: True)
    monkeypatch.setattr(runtime, "is_model_cached", lambda: False)
    monkeypatch.setattr(service, "_get_kokoro", lambda: pytest.fail("stats must not load models"))
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "tts_provider": "local", "tts_model": "kokoro", "tts_voice": "af_heart",
    })
    assert service.available
    stats = service.get_stats()
    # The browser gates the speak button on available AND ready; an unloaded
    # model is still ready for a first explicit synthesis request.
    assert stats["available"] and stats["ready"]
    assert not stats["model_loaded"]
    assert not stats["model_cached"]
    assert service._kokoro is None


def test_missing_local_package_has_actionable_stats_without_model_load(tmp_path, monkeypatch):
    service = TTSService(cache_dir=tmp_path)
    monkeypatch.setattr(runtime, "is_installed", lambda: False)
    monkeypatch.setattr(runtime, "is_model_cached", lambda: False)
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "tts_provider": "local", "tts_model": "kokoro", "tts_voice": "af_heart",
    })
    stats = service.get_stats()
    assert not stats["available"]
    assert "requirements-optional.lock" in stats["error"]
    assert service._kokoro is None


@pytest.mark.asyncio
async def test_slow_local_synthesis_keeps_event_loop_responsive():
    import asyncio
    from fastapi import FastAPI
    from routes.tts_routes import setup_tts_routes

    entered = threading.Event()
    release = threading.Event()

    class SlowSpeech:
        available = True

        def synthesize(self, text):
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("event loop blocked during synthesis")
            return b"RIFFtest"

    app = FastAPI()
    app.include_router(setup_tts_routes(SlowSpeech()))

    @app.get("/heartbeat")
    async def heartbeat():
        return {"alive": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/api/tts/synthesize", json={"text": "hello"}))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            response = await asyncio.wait_for(client.get("/heartbeat"), timeout=1)
            assert response.json() == {"alive": True}
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 200
