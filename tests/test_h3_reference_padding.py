"""Reference endings must reach H3 conditioning without rounding away footage."""

from types import SimpleNamespace

import pytest
import torch

from scripts import h3_video_worker as worker
from tests.test_h3_vfx_worker import pinned_reference_nodes


def reference(count, *, audio=True, extra_samples=0):
    frames = torch.arange(count, dtype=torch.float32).reshape(count, 1, 1, 1).expand(count, 32, 32, 3) / count
    if not audio:
        return frames, None, count / worker.FPS, 0
    # A distinct end-of-track marker catches lost dialogue/end sounds. Allow a
    # sub-frame tail: resampling a clip to 24fps need not land on its last sample.
    samples = round(count / worker.FPS * worker.AUDIO_RATE) + extra_samples
    waveform = torch.zeros((1, 2, samples))
    waveform[..., -512:] = .75
    return frames, {"waveform": waveform, "sample_rate": worker.AUDIO_RATE}, count / worker.FPS, samples / worker.AUDIO_RATE


def prepare(monkeypatch, references, *, target=362):
    config = {"mode": "ref2va", "loras": [], "width": 64, "height": 32, "frames": target,
              "reference_size": "match", "prompt": "Keep the ending of <Video 1>."}
    media = {"first_frame": None, "last_frame": None, "reference_images": [],
             "reference_videos": list(references), "reference_audio": []}
    monkeypatch.setattr(worker, "read_video", references.__getitem__)
    return worker.prepare_media(media, config), config


@pytest.mark.parametrize("count,target,expected", [
    (48, 124, 56), (73, 124, 73), (124, 124, 124), (125, 141, 141),
    (191, 192, 192), (200, 209, 209), (345, 362, 345), (360, 362, 362),
    (200, 124, 124),
])
def test_complete_reference_padded_up_with_explicit_output_limit(monkeypatch, count, target, expected):
    original, soundtrack, duration, sound_duration = reference(count)
    original_audio = soundtrack["waveform"].clone()
    prepared, config = prepare(monkeypatch, {"clip": (original, soundtrack, duration, sound_duration)}, target=target)
    frames = prepared["ref_videos"]["ref_video_1"]
    audio = prepared["ref_video_audios"]["ref_video_audio_1"]["waveform"]
    retained = min(count, target)
    assert len(frames) == expected <= target and (expected - 5) % 17 == 0
    assert torch.equal(frames[:retained], original[:retained])
    if expected > retained:
        assert torch.equal(frames[retained:], original[retained - 1:retained].expand(expected - retained, 32, 32, 3))
    samples = round(expected / worker.FPS * worker.AUDIO_RATE)
    retained_samples = min(samples, original_audio.shape[-1])
    assert torch.equal(audio[..., :retained_samples], original_audio[..., :retained_samples])
    assert audio.shape[-1] == samples
    assert audio[..., retained_samples:].count_nonzero() == 0
    assert torch.equal(soundtrack["waveform"], original_audio), "Preparing references must not mutate their original soundtrack"
    assert config["frames"] == target, "Reference padding must not change requested output duration"


def test_multiple_references_align_independently_and_keep_soundtrack_pairing(monkeypatch):
    first = reference(191)
    second = reference(72)
    silent = reference(48, audio=False)
    prepared, _ = prepare(monkeypatch, {"first": first, "second": second, "silent": silent})
    for number, original, expected in ((1, first, 192), (2, second, 73), (3, silent, 56)):
        frames = prepared["ref_videos"][f"ref_video_{number}"]
        assert len(frames) == expected
        assert torch.equal(frames[:len(original[0])], original[0])
        sound = prepared["ref_video_audios"].get(f"ref_video_audio_{number}")
        if original[1] is None:
            assert sound is None
        else:
            samples = original[1]["waveform"].shape[-1]
            assert torch.equal(sound["waveform"][..., :samples], original[1]["waveform"])
            assert sound["waveform"][..., samples:].count_nonzero() == 0


@pytest.mark.parametrize("count,aligned", [(120, 124), (192, 192), (360, 362)])
def test_reference_durations_and_padding_are_independent(monkeypatch, count, aligned):
    prepared, _ = prepare(monkeypatch, {str(i): reference(count) for i in range(3)})
    assert [len(frames) for frames in prepared["ref_videos"].values()] == [aligned] * 3
    assert all(audio["waveform"].shape[-1] == round(aligned / 24 * worker.AUDIO_RATE)
               for audio in prepared["ref_video_audios"].values())


@pytest.mark.parametrize("seed_hunter", [False, True])
def test_pinned_core_keeps_three_full_references_with_three_standalone_audio_files(monkeypatch, seed_hunter):
    from scripts import h3_seed_hunter as hunter
    from scripts.h3_seed_hunter_controls import validate_options

    references = {f"video-{i}": reference(360) for i in range(3)}
    standalone = {f"audio-{i}": {"waveform": torch.full((1, 2, 15 * worker.AUDIO_RATE), (i + 1) / 4),
                                "sample_rate": worker.AUDIO_RATE} for i in range(3)}
    prepared, config = prepare(monkeypatch, references)
    media = {"first_frame": None, "last_frame": None, "reference_images": [],
             "reference_videos": list(references), "reference_audio": list(standalone),
             "reference_mods": []}
    monkeypatch.setattr(worker, "read_audio", lambda path: (standalone[path], 15))
    if seed_hunter:
        config.update(recipe="seed_hunter", seed_hunter=validate_options({"stage": "single"}))
        prepared = hunter.prepare_media({}, config, media, worker.prepare_media, None)
    else:
        prepared = worker.prepare_media(media, config)
    core, clip, vae, audio_vae, calls = pinned_reference_nodes()
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    runtime.h3 = core
    if seed_hunter:
        positive, latent, _ = hunter.condition(runtime, config, prepared, clip, vae, audio_vae)
    else:
        positive, latent = runtime.condition(config, prepared, clip, vae, audio_vae)
    blocks = positive[0][1]["minimax_refs"]
    assert [block["kind"] for block in blocks] == ["video_audio"] * 3 + ["audio"] * 3
    assert [item["type"] for item in calls["tokenize"][1]["minimax_ref_items"]] == ["audio", "video"] * 3 + ["audio"] * 3
    assert [block["latent_t"] for block in blocks[:3]] == [core.video_latent_t(362)] * 3
    assert [block["ref_audio_t"] for block in blocks[:3]] == [round(362 / 24 * 40)] * 3
    assert [block["ref_audio_t"] for block in blocks[3:]] == [600] * 3
    for index, block in enumerate(blocks[3:]):
        assert float(block["audio_latent"].mean()) == pytest.approx((index + 1) / 4)
    assert latent["samples"].tensors[0].shape[2] == core.video_latent_t(362)


def test_pinned_core_receives_original_final_frame_and_end_audio_marker(monkeypatch):
    source = reference(191, extra_samples=300)
    prepared, config = prepare(monkeypatch, {"clip": source}, target=192)
    core, clip, original_vae, original_audio_vae, _ = pinned_reference_nodes()
    captured = {}

    def encode_video(frames):
        captured["frames"] = frames.clone()
        return original_vae.encode(frames)

    def encode_audio(waveform):
        captured["waveform"] = waveform.movedim(-1, 1).clone()
        return original_audio_vae.encode(waveform)

    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    runtime.h3 = core
    positive, latent = runtime.condition(config, prepared, clip, SimpleNamespace(encode=encode_video),
                                         SimpleNamespace(encode=encode_audio))
    frames, audio = captured["frames"], captured["waveform"]
    assert frames.device.type == audio.device.type == "cpu"
    assert len(frames) == 192, "The upstream floor must not discard the final 16 real frames"
    assert torch.equal(frames[:191], source[0])
    assert torch.equal(frames[191:], source[0][-1:])
    samples = source[1]["waveform"].shape[-1]
    assert torch.equal(audio[..., :samples], source[1]["waveform"])
    assert torch.all(audio[..., samples - 512:samples] == .75), "The final sound must reach the audio VAE"
    assert audio[..., samples:].count_nonzero() == 0
    assert audio.shape[-1] == 8 * worker.AUDIO_RATE
    assert positive[0][1]["minimax_refs"][0]["kind"] == "video_audio"
    assert latent["samples"].tensors[0].shape[2] == core.video_latent_t(192)
    assert config["frames"] == 192 and not worker.is_vfx_edit(config)
