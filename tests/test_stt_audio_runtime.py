"""Exercise the actual optional audio decoder without downloading a model."""

import io
import wave

import numpy as np
import pytest


def test_faster_whisper_can_decode_audio_with_locked_pyav():
    audio = pytest.importorskip("faster_whisper.audio")
    samples = (np.sin(np.arange(1600) * 0.1) * 16000).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    buffer.seek(0)

    decoded = audio.decode_audio(buffer, sampling_rate=16000)

    assert decoded.shape == (1600,)
    assert decoded.dtype == np.float32
    assert np.isfinite(decoded).all()
    assert np.max(np.abs(decoded)) > 0.1
