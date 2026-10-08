"""VFX source guides and optional native references survive HTTP boundaries."""
import base64
import json
from pathlib import Path

import pytest

from src import h3_video as h3
from src.h3_vfx import normalize_vfx_inputs
from tests.test_h3_video import api as generation_api, inventory, weights, png, config
from tests.test_h3_vfx_install import vfx_inventory, validate
from tests.test_h3_prompt_enhancement import (
    api, endpoints, upstream, inference, payload, image_bytes, image_request,
)


FFP = "minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors"


def test_ffp_is_discovered_as_a_vfx_adapter_and_accepts_native_edited_image(vfx_inventory, tmp_path):
    inv, raw = vfx_inventory
    path = weights(tmp_path / "models--author--MiniMax-H3" / "snapshots" / "revision" / FFP, True)
    inv["components"] = h3.discover_components([tmp_path])
    adapter = next(item for item in inv["components"] if item["name"] == path.name)
    assert adapter["role"] == "lora" and adapter["recipe"] == "vfx_edit"
    assert adapter["variant"] == "ref2va"
    accepted = h3.validate_config({**raw, "loras": [{"id": adapter["id"], "strength": 1}],
        "prompt": "Propagate the edited jacket in <Picture 1> through the source clip."},
        inv["components"], inv["gpus"], {"source_video": [None], "reference_images": [None]})
    assert accepted["loras"][0]["path"] == adapter["path"]
    assert accepted["prompt"].startswith("vfx_edit: ")
    assert "<Picture 1>" in accepted["prompt"]


@pytest.mark.parametrize("mode", ["t2va", "ref2va", "fl2va"])
def test_ordinary_h3_modes_reject_an_unapplied_source_guide(inventory, mode):
    uploads = {"source_video": [None], **({"reference_images": [None]} if mode == "ref2va" else
                                         {"first_frame": [None]} if mode == "fl2va" else {})}
    with pytest.raises(ValueError, match="source video|Source video|VFX"):
        h3.validate_config(config(inventory, mode), inventory["components"], inventory["gpus"], uploads)


@pytest.mark.parametrize("field,limit", [("source_video", 1), ("reference_images", 9),
                                       ("reference_videos", 3), ("reference_audio", 3)])
def test_reference_count_limits_still_apply(vfx_inventory, field, limit):
    uploads = {"source_video": [None], field: [None] * (limit + 1)}
    with pytest.raises(ValueError):
        validate(vfx_inventory, uploads)


def test_source_is_separate_from_native_twelve_reference_limit(vfx_inventory):
    accepted = validate(vfx_inventory, {"source_video": [None], "reference_images": [None] * 9,
                                       "reference_videos": [None] * 3})
    assert accepted["prompt"].startswith("vfx_edit:")
    with pytest.raises(ValueError, match="12 reference"):
        validate(vfx_inventory, {"source_video": [None], "reference_images": [None] * 9,
                                 "reference_videos": [None] * 3, "reference_audio": [None]})


def test_input_normalization_is_nonmutating_and_only_migrates_absent_source(vfx_inventory):
    saved = validate(vfx_inventory, {"source_video": [None]})
    guide, image, ref = object(), object(), object()
    old = {"reference_videos": [guide], "reference_images": [image]}
    migrated = normalize_vfx_inputs(saved, old)
    assert old == {"reference_videos": [guide], "reference_images": [image]}
    assert migrated["source_video"] == [guide] and migrated["reference_videos"] == []
    assert migrated["reference_images"] == [image]
    for source in ([], [guide]):
        explicit = {"source_video": source, "reference_videos": [ref]}
        assert normalize_vfx_inputs(saved, explicit) == explicit
    disabled = {**saved, "loras": []}
    assert normalize_vfx_inputs(disabled, old) == old


@pytest.mark.parametrize("legacy", [False, True])
def test_multipart_stores_source_separately_and_preserves_native_files(
        generation_api, vfx_inventory, monkeypatch, legacy):
    client, manager, _ = generation_api
    _, raw = vfx_inventory
    captured = {}
    source_field = "reference_videos" if legacy else "source_video"
    files = [(source_field, ("source.mp4", b"source-video", "video/mp4")),
             ("reference_images", ("edited-frame.png", png(), "image/png")),
             ("reference_audio", ("style.wav", b"reference-audio", "audio/wav"))]
    if not legacy:
        files.append(("reference_videos", ("motion.mp4", b"native-motion", "video/mp4")))
    def launch(directory, owner, cfg, uploads):
        captured["config"] = cfg
        captured["source_is_scalar"] = isinstance(uploads["source_video"], str)
        captured["source"] = Path(uploads["source_video"]).read_bytes()
        captured["refs"] = {field: [Path(path).read_bytes() for path in uploads.get(field, [])]
                            for field in ("reference_images", "reference_videos", "reference_audio")}
        captured["metadata"] = json.loads((directory / "submission.json").read_text())
        assert owner == "corey"
        return {"id": directory.name, "status": "queued"}
    monkeypatch.setattr(manager, "launch", launch)
    response = client.post("/api/video/h3/jobs", data={"config": json.dumps(raw)}, files=files)
    assert response.status_code == 201, response.text
    assert captured["source_is_scalar"] and captured["source"] == b"source-video"
    assert captured["refs"] == {"reference_images": [png()], "reference_audio": [b"reference-audio"],
                                "reference_videos": [] if legacy else [b"native-motion"]}
    assert captured["config"]["prompt"].startswith("vfx_edit:")
    assert captured["metadata"]["input_names"]["source_video"] == ["source.mp4"]
    assert captured["metadata"]["input_names"]["reference_images"] == ["edited-frame.png"]


@pytest.mark.parametrize("files", [
    [("reference_images", ("image.png", png(), "image/png"))],
    [("source_video", ("one.mp4", b"one", "video/mp4")),
     ("source_video", ("two.mp4", b"two", "video/mp4"))],
    [("source_video", ("source.txt", b"source", "text/plain"))],
])
def test_bad_source_uploads_fail_without_starting_a_worker(generation_api, vfx_inventory, monkeypatch, files):
    client, manager, _ = generation_api
    _, raw = vfx_inventory
    monkeypatch.setattr(manager, "launch", lambda *args: pytest.fail("Invalid source must not launch"))
    response = client.post("/api/video/h3/jobs", data={"config": json.dumps(raw)}, files=files)
    assert response.status_code == 400, response.text


def test_vfx_enhancement_receives_native_image_pixels_and_distinct_reference_roles(api, endpoints, upstream, inference):
    endpoints.add("local")
    upstream.state["capabilities"] = ["completion", "multimodal"]
    text = "Use the jacket in <Picture 1>, the motion in <Video 1> and the style in <Audio 1>; preserve the source camera."
    inference.state["text"] = "vfx_edit: " + text
    data = image_bytes("purple")
    response = image_request(api, payload(prompt=text, mode="ref2va", recipe="vfx_edit", frames=73,
        reference_counts={"source_video": 1, "reference_images": 1, "reference_videos": 1, "reference_audio": 1}),
        [("reference_images", ("edited-frame.png", data, "image/png"))])
    assert response.status_code == 200, response.text
    assert response.json()["prompt"] == inference.state["text"]
    messages = inference.seen[0]["messages"]
    parts = messages[1]["content"]
    context = json.loads(parts[0]["text"])
    assert context["reference_counts"]["source_video"] == 1
    assert context["reference_counts"]["reference_videos"] == 1
    assert context["observed_images"] == [{"image_index": 1, "marker": "<Picture 1>", "role": "reference_images"}]
    assert "REFERENCE IMAGE" in parts[1]["text"]
    assert base64.b64decode(parts[2]["image_url"]["url"].split(",", 1)[1]) == data
    assert "aligned video guide" in messages[0]["content"]
    assert "Video and audio contents are NOT supplied" in messages[0]["content"]
    assert "Do not introduce\nPicture, Video, Audio" not in messages[0]["content"]


def test_legacy_vfx_enhancement_can_send_images_with_promoted_source_counts(api, endpoints, inference):
    endpoints.add("local")
    inference.state["text"] = "vfx_edit: Propagate the edited jacket in <Picture 1>; preserve the camera."
    response = image_request(api, payload(mode="ref2va", recipe="vfx_edit", prompt="Use the edited first frame.",
        reference_counts={"reference_videos": 1, "reference_images": 1}),
        [("reference_images", ("edited.png", image_bytes(), "image/png"))])
    assert response.status_code == 200, response.text
    context = json.loads(inference.seen[0]["messages"][1]["content"][0]["text"])
    assert context["reference_counts"]["source_video"] == 1
    assert context["reference_counts"]["reference_videos"] == 0


def test_explicit_no_source_keeps_video_as_native_context_for_enhancement(api, endpoints, inference):
    endpoints.add("local")
    inference.state["text"] = "vfx_edit: Use motion from <Video 1> while keeping the source setting."
    response = api.post("/api/video/h3/enhance-prompt", json=payload(mode="ref2va", recipe="vfx_edit",
        reference_counts={"source_video": 0, "reference_videos": 1}))
    assert response.status_code == 200, response.text
    context = json.loads(inference.seen[0]["messages"][1]["content"])
    assert context["reference_counts"]["source_video"] == 0
    assert context["reference_counts"]["reference_videos"] == 1


def test_vfx_can_enhance_before_source_upload(api, endpoints, inference):
    endpoints.add("local")
    inference.state["text"] = "vfx_edit: Make the jackets purple while preserving the take."
    response = api.post("/api/video/h3/enhance-prompt", json=payload(mode="ref2va", recipe="vfx_edit"))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("bad_counts", [{"source_video": 2}, {"source_video": 1, "first_frame": 1},
                                       {"source_video": 1, "reference_images": 10}])
def test_invalid_vfx_enhancement_counts_fail_before_inference(api, endpoints, inference, bad_counts):
    endpoints.add("local")
    response = api.post("/api/video/h3/enhance-prompt", json=payload(
        mode="ref2va", recipe="vfx_edit", reference_counts=bad_counts))
    assert response.status_code == 400 and not inference.seen
