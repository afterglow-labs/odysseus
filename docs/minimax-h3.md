# MiniMax H3 video in Odysseus

Odysseus runs MiniMax H3 as a local, one-job Python worker. The UI, job queue,
uploads, progress, cancellation and MP4 delivery belong to Odysseus. It imports
the open-source ComfyUI inference library internally, pinned to commit
`5c460d8172fe30761ff67c0df3d5643bb74e0d70`; it does not start a ComfyUI server,
open a node editor, execute workflow JSON, or load custom nodes.

The dependency is intentional: the pinned core implements H3's joint audio/video
latents, Qwen3-VL conditioning, reference modalities and separate noise shifts.
Calling a still-image Diffusers pipeline cannot substitute for these operations.
The core's original license remains in `runtimes/minimax-h3/ComfyUI/LICENSE`.

## Environment and model files

From the WSL checkout:

```bash
./venv/bin/python scripts/setup_h3_runtime.py
```

Setup uses that Python environment and a project-local runtime checkout. It
installs the inference dependencies in `requirements-h3.txt`, preserves an
existing modified core checkout, and does not download model weights. It does
not deliberately replace the installed PyTorch/CUDA build. CUDA-capable PyTorch
and a compatible torchvision must already be present. Setup refuses global
Python. `--skip-dependencies` prepares/verifies only the source checkout.

H3 needs four compatible **Comfy-format `.safetensors` files**:

1. MiniMax H3 diffusion transformer.
2. H3 Qwen3-VL-32B text/vision encoder.
3. H3 video VAE.
4. H3 audio VAE.

An optional compatible H3 LoRA augments the transformer. A LoRA is not a base
model. GGUF, arbitrary Transformers model directories, and complete Diffusers
repositories are not accepted by this worker. NVFP4 transformer files require a
Blackwell GPU, such as the RTX 5090; the worker rejects those files on the RTX
4090. Use another supported precision for older GPUs. Select a physical GPU by
UUID when available: `nvidia-smi` indices and PyTorch indices may differ on WSL.

## Modes and inputs

| Mode | Inputs | Result |
| --- | --- | --- |
| Text to video (`t2va`) | Prompt | Video and generated audio |
| First/last frames (`fl2va`) | Prompt, first image and/or last image | Video anchored to the keyframes, with generated audio |
| References (`ref2va`) | Prompt, reference image or video; optional audio references | Reference-conditioned video and generated audio |

Reference mode supports up to nine images, three videos and three audio files,
with at most twelve uploaded references overall. Each reference clip/audio file
must be 2–15 seconds; videos together must total at most 15 seconds, and all audio
(including video soundtracks) must total at most 15 seconds. Audio-only reference
requests are rejected. Reference video is resampled to 24 fps, and its soundtrack
is paired with the same video. Audio is converted to stereo 32 kHz.

Use `<Picture 1>`, `<Video 1>` and `<Audio 1>` in reference prompts. Numbering is
one-based per type. Images come first, then videos, then standalone audio. A
video soundtrack consumes an audio index before any standalone audio references.

Images use formats supported by Pillow, including PNG/JPEG/WebP. Video and
audio use codecs supported by the installed PyAV/FFmpeg build; ordinary H.264
MP4/MOV, WebM, WAV, FLAC and MP3 are appropriate inputs. The API may impose a
narrower upload extension list. Unreadable media fails before GPU weights load.
The output is a browser-compatible H.264/AAC MP4 with stereo generated audio.

## Controls

- Width/height: multiples of 32; area at most `768 × 1344` pixels.
- Frames: aligned upward to `17k + 5`, at 24 fps. The default 124 frames is about
  5.17 seconds; 362 frames is about 15.08 seconds. The low-level worker accepts
  shorter smoke-test clips, but H3's documented trained range starts at 124.
- Steps: 1–100; default 20. Very low steps are useful for a smoke test, not for
  judging normal output quality.
- Seed: unsigned 64-bit integer, passed to noise generation and sampling.
  Identical settings preserve the seed; exact bitwise output can vary with
  device/kernel versions.
- Sampler: Euler or res_multistep. Scheduler: simple or normal.
- Video/audio shifts: separate controls, defaults 12 and 3.
- Reference image size: `match` caps reference area to the output area; `max`
  allows more reference detail and can use much more memory and time.
- Optional LoRA strength: default 1. An incompatible LoRA that maps no weights
  fails instead of silently doing nothing.

H3 uses fixed CFG 1 in this adapter. There is no negative-prompt control because
this path does not use negative conditioning. This initial integration does not
implement H3 Fun Control patches, arbitrary per-frame guide tracks, or a BFS
head-swap preset; those are separate capabilities.

The worker exits after each finished or failed job, releasing its CUDA context.
Stopping a job terminates its worker process; it does not unload unrelated model
servers. Cold starts include model loading and prompt encoding. Progress reports
those phases separately from denoising steps, video/audio decode and MP4 encode.

## Worker contract and validation

The backend writes a private job manifest and launches:

```bash
venv/bin/python scripts/h3_video_worker.py --job /absolute/path/manifest.json
```

The manifest has `config`, `uploads`, `runtime_path`, `output_path` and
`status_path`. Config includes the four absolute component paths, selected GPU,
mode, prompt and controls above. Upload keys are `first_frame`, `last_frame`,
`reference_images`, `reference_videos`, and `reference_audio`; the latter three
are arrays. Top-level upload keys are also accepted for compatibility with the
API supervisor. Output is an absolute `.mp4` path.

Status JSON is replaced atomically and contains `phase`, `step`, `total_steps`
and elapsed time. Completed status includes frames, fps and duration. Failed
status contains an error and the process exits nonzero. The MP4 becomes visible
at the requested output path only after encoding finishes successfully.

Run CPU tests without loading model weights:

```bash
venv/bin/python -m pytest -q tests/test_h3_worker.py
```

These cover invalid jobs, mode/reference argument mapping, seed/CFG propagation,
progress and failure semantics, real RGB H.264/AAC muxing, stereo preservation,
30-to-24 fps reference conversion, 44.1-to-32 kHz audio conversion, and duration
limits. Real GPU generation is a separate integration check.

Integration evidence (2026-10-06): all three modes completed real RTX 5090
generation through the API supervisor and worker, using isolated FastAPI tests
and a temporary database. Each produced 124 frames at 512×288 and 24 fps with
stereo 32 kHz AAC audio, using four denoising steps:

- `t2va` with the cached pruned NVFP4 FL2VA transformer.
- `fl2va` with only a last-frame image, using that same transformer.
- `ref2va` with a reference image and a reference video carrying its soundtrack,
  using the cached pruned NVFP4 Ref2VA transformer; this run took 46.85 seconds.

Owner-scoped playback and Gallery persistence passed through the API tests. A
separate mixed NVFP4 transformer run with a synthetic orange-ball image completed
in 53.27 seconds; the rendered frames reflected the image and GPU memory returned
to its pre-job baseline after exit. These are inference and delivery smoke tests.
They do not establish production quality at four steps, or validate every
reference/LoRA/model-precision combination.

Upstream implementation: [H3 inference nodes](https://github.com/Comfy-Org/ComfyUI/blob/5c460d8172fe30761ff67c0df3d5643bb74e0d70/comfy_extras/nodes_minimax_h3.py).
