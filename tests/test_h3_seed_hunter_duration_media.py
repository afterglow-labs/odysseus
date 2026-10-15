"""Real encoded candidate durations stay short when the final target is longer.

Inference is stubbed; source probing, snapshot storage and MP4 mux/decode are
real CPU operations. This test never initializes a GPU or launches a job.
"""
import json
from pathlib import Path

import av
import pytest
import torch

from scripts import h3_seed_hunter as hunter, h3_video_worker as worker
from src.h3_video_length import select_batch_video_length
from tests.test_h3_seed_hunter_worker import manifest


def test_three_short_mp4_candidates_with_independent_full_source_target(manifest, monkeypatch, tmp_path):
    source = tmp_path / "reference.mp4"
    audio = {"waveform": torch.zeros((1, 2, 1)), "sample_rate": 32000}
    worker.mux_mp4(source, torch.zeros((295, 16, 16, 3)), audio)
    manifest["config"].update(frames=107, seed_hunter={"stage": "preview", "preview_count": 3})
    chosen, length = select_batch_video_length(manifest["config"], {"reference_videos": [str(source)]})
    assert chosen["frames"] == 107
    assert chosen["seed_hunter"]["final_frames"] == 311
    assert length["duration_seconds"] == pytest.approx(295 / 24)
    assert length["target"] == "final"
    manifest["config"] = chosen
    manifest["reference_videos"] = [str(source)]
    calls = []

    class Runtime:
        def __init__(self, *_): pass
        def load(self, config, _progress):
            calls.append(("load", config["frames"]))
            return (object(),) * 4
        def cleanup_model_state(self): pass
        def decode(self, *_): return torch.zeros((107, 16, 16, 3)), audio

    def sample(_runtime, config, *_args):
        calls.append(("sample", config["frames"], config["seed"]))
        return {"samples": torch.zeros(1)}, {"samples": torch.ones(1)}

    monkeypatch.setattr(hunter, "patch_model", lambda _r, _c, model, _path: model)
    monkeypatch.setattr(hunter, "condition", lambda *_: (
        [[torch.ones(1), {}]], {"samples": torch.zeros(1)}, [[torch.ones(1), {}]]))
    monkeypatch.setattr(hunter, "sample_candidate", sample)
    worker.run_job(manifest, runtime_factory=Runtime, media_loader=lambda *_: {})
    assert calls == [("load", 107), ("sample", 107, 42), ("sample", 107, 1042), ("sample", 107, 2042)]
    folder = Path(manifest["output_path"]).parent
    candidates = json.loads((folder / "candidates.json").read_text())["candidates"]
    assert len(candidates) == 3
    for row in candidates:
        with av.open(str(folder / row["video"])) as video:
            stream = video.streams.video[0]
            assert stream.frames == 107
            assert float(stream.duration * stream.time_base) == pytest.approx(107 / 24)
            assert sum(1 for _ in video.decode(video=0)) == 107
        metadata = json.loads((folder / row["metadata"]).read_text())
        assert metadata["config"]["frames"] == 107
        assert metadata["config"]["seed_hunter"]["final_frames"] == 311
