"""Kokoro inference using the published Python 3.14 sherpa-onnx wheels.

The versioned upstream model bundle is verified before it is extracted. No
model downloads happen unless the local TTS provider is actually selected.
"""

import hashlib
import importlib.util
import io
import logging
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import threading
import wave

import httpx

from src.constants import TTS_MODEL_DIR

logger = logging.getLogger(__name__)

MODEL_NAME = "kokoro-multi-lang-v1_0"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
    f"{MODEL_NAME}.tar.bz2"
)
# GitHub's tts-models release asset digest; changing models requires updating
# the voice IDs, size, and digest together.
MODEL_SHA256 = "c5f7e2d2caf082bc1d20fb70334a61d99d20b484500aad32e7cf84c128ea3298"
MODEL_DOWNLOAD_BYTES = 349_906_910
MODEL_EXTRACT_MAX_BYTES = 600_000_000
MODEL_MAX_FILES = 4096
_MODEL_LOCK = threading.Lock()

# Official speaker IDs for this exact bundle. The former KPipeline(lang_code
# = "a") used English phonemes, so preserve English pronunciation and names.
# https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html
VOICE_IDS = {name: index for index, name in enumerate((
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica",
    "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella",
    "bf_lily", "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
))}


def is_installed() -> bool:
    """Check optional package presence without importing native code or models."""
    return importlib.util.find_spec("sherpa_onnx") is not None


def is_model_cached() -> bool:
    return _model_ready(Path(TTS_MODEL_DIR) / MODEL_NAME)


def _model_ready(path: Path) -> bool:
    return all((path / name).is_file() and (path / name).stat().st_size > 0 for name in (
        "model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt",
        "espeak-ng-data/phontab", "LICENSE",
    ))


def _extract_model(archive: Path, destination: Path) -> None:
    """Extract only bounded regular files below the expected bundle root."""
    total_size = 0
    with tarfile.open(archive, "r:bz2") as bundle:
        for count, member in enumerate(bundle, start=1):
            name = PurePosixPath(member.name)
            if (count > MODEL_MAX_FILES or name.is_absolute()
                    or ".." in name.parts or "\\" in member.name
                    or not name.parts or name.parts[0] != MODEL_NAME
                    or not (member.isdir() or member.isfile())):
                raise ValueError("Unsafe Kokoro model archive member")
            total_size += member.size
            if member.size < 0 or total_size > MODEL_EXTRACT_MAX_BYTES:
                raise ValueError("Kokoro model archive exceeds extraction limit")
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)


def ensure_model(model_root: Path) -> Path:
    """Return the complete cached bundle, downloading atomically if absent."""
    target = model_root / MODEL_NAME
    with _MODEL_LOCK:
        if _model_ready(target):
            return target
        if target.exists():
            raise RuntimeError(f"Incomplete Kokoro model cache; remove {target} and retry")
        model_root.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading Kokoro model (350 MB) to %s", target)
        with tempfile.TemporaryDirectory(prefix=".kokoro-", dir=model_root) as temporary:
            staging = Path(temporary)
            archive = staging / "model.tar.bz2"
            digest = hashlib.sha256()
            size = 0
            with httpx.stream("GET", MODEL_URL, follow_redirects=True,
                              timeout=httpx.Timeout(60, connect=15)) as response:
                response.raise_for_status()
                with archive.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        size += len(chunk)
                        if size > MODEL_DOWNLOAD_BYTES:
                            raise ValueError("Kokoro model download exceeds expected size")
                        digest.update(chunk)
                        output.write(chunk)
            if size != MODEL_DOWNLOAD_BYTES or digest.hexdigest() != MODEL_SHA256:
                raise ValueError("Kokoro model checksum or size mismatch")
            _extract_model(archive, staging)
            complete = staging / MODEL_NAME
            if not _model_ready(complete):
                raise ValueError("Kokoro model archive is missing required files")
            try:
                complete.rename(target)
            except OSError:
                # Another process may have completed the same verified bundle.
                if not _model_ready(target):
                    raise
        return target


class KokoroPipeline:
    """Local Kokoro on CPU; CUDA is not required on any supported platform."""

    def __init__(self, model_root=None):
        self.pipeline = None
        self.available = False
        self.device = "cpu"
        self.error = None
        self._synthesis_lock = threading.Lock()
        try:
            import sherpa_onnx

            path = ensure_model(Path(model_root) if model_root is not None else Path(TTS_MODEL_DIR))
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=str(path / "model.onnx"),
                        voices=str(path / "voices.bin"),
                        tokens=str(path / "tokens.txt"),
                        data_dir=str(path / "espeak-ng-data"),
                        lexicon=str(path / "lexicon-us-en.txt"),
                        lang="en-us",
                    ),
                    provider="cpu", num_threads=2,
                ),
                max_num_sentences=1,
            )
            if not config.validate():
                raise ValueError("Invalid Kokoro model configuration")
            self.pipeline = sherpa_onnx.OfflineTts(config)
            self.available = True
            logger.info("Kokoro-82M TTS loaded on CPU")
        except ImportError:
            self.error = ("Local TTS requires: python -m pip install --require-hashes "
                          "-r requirements.lock -r requirements-optional.lock")
            logger.warning(self.error)
        except Exception as exc:
            self.error = f"Kokoro initialization failed: {exc}"
            logger.error(self.error, exc_info=True)

    def synthesize_raw(self, text: str, voice: str = "af_heart", speed: float = 1.0):
        if not self.available or not text.strip():
            return None
        # 'alloy' is the endpoint-provider default stored in existing settings.
        voice = "af_alloy" if voice == "alloy" else voice
        if not isinstance(voice, str) or voice not in VOICE_IDS:
            logger.warning("Unknown English Kokoro voice: %s", voice)
            return None
        try:
            import numpy as np

            with self._synthesis_lock:
                audio = self.pipeline.generate(text, sid=VOICE_IDS[voice], speed=speed)
            samples = np.asarray(audio.samples)
            if samples.size == 0 or not np.isfinite(samples).all():
                return None
            buf = io.BytesIO()
            with wave.open(buf, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(audio.sample_rate)
                output.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
            return buf.getvalue()
        except Exception:
            logger.error("Kokoro synthesis failed", exc_info=True)
            return None
