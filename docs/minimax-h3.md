# MiniMax H3 video in Odysseus

Odysseus runs MiniMax H3 with a local Python worker for each job. The UI, job queue,
uploads, progress, cancellation and MP4 delivery belong to Odysseus. It imports
the open-source ComfyUI inference library internally, pinned to commit
`5c460d8172fe30761ff67c0df3d5643bb74e0d70`; it does not start a ComfyUI server,
open a node editor, execute workflow JSON, or load custom nodes.

The dependency is intentional: the pinned core implements H3's joint audio/video
latents, Qwen3-VL conditioning, reference modalities and separate noise shifts.
Calling a still-image Diffusers pipeline cannot substitute for these operations.
The core's original license remains in `runtimes/minimax-h3/ComfyUI/LICENSE`.

## Queue controls and rerunning jobs

**Queue controls** appears above the recent jobs in both H3 and BFS panels.
**Pause queue** lets the current render finish and holds your waiting H3 and BFS
jobs until **Resume queue**. The pause is saved on the server and survives closing
the panel or restarting Odysseus.

Choose this panel's queued jobs or all H3 and BFS queued jobs before using
**Cancel queued jobs** or **Delete queued jobs**. Cancel keeps the saved inputs,
parameters and job records for rerunning. Delete cancels the waiting jobs and
permanently removes their records and files after confirmation. Neither action
changes a job that has already started rendering.

**Edit queued parameters** updates the selected parameters on every queued job
in this panel. Check each parameter you want to change; unchecked values, input
files, workflow modes and queue positions stay as saved. Pause first if you want
the target jobs to remain queued while you edit. A job that starts or is edited
elsewhere causes a conflict instead of silently receiving stale changes.

**Run again with new parameters** creates new jobs using each original job's
saved inputs. Choose finished jobs, queued jobs, or all jobs; review the count,
check any parameters to change and confirm. The new copies join the end of the
shared queue, preserving original records and outputs. Reruns reuse server-side
input files without uploading them again. A paused queue stays paused.

## Editing queued jobs

Queued jobs have an **Edit** button. Change the prompt, models, GPU placement,
render settings, or input media, then choose **Save changes**. Saved inputs stay
attached without another upload unless removed or replaced. The job keeps its
ID and queue position. **Cancel edit** restores the previous new-job draft.
Multiple reference videos still belong to one job; editing never expands them
into a batch. The queue keeps running while the editor is open. A job that starts
or changes in another client rejects the stale save and leaves the edits visible.
Running and finished jobs cannot be edited.

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

### Installed H3 defaults

An administrator can install an optional `h3_video_defaults.json` in Odysseus's
data directory (`data/` in this checkout, or `ODYSSEUS_DATA_DIR`). Component
selections accept scanned component IDs or exact cached file paths. For example:

```json
{
  "format_version": 1,
  "id": "lightx2v-ref2v-turbo-8step-v1.0",
  "name": "LightX2V H3 Ref2V Turbo · 8 steps",
  "config": {
    "mode": "ref2va",
    "model": "/path/to/MiniMax_H3_Ref2VA.safetensors",
    "encoder": "/path/to/qwen3vl_minimax_h3.safetensors",
    "video_vae": "/path/to/minimax_h3_video_vae.safetensors",
    "audio_vae": "/path/to/minimax_h3_audio_vae.safetensors",
    "lora": "/path/to/minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors",
    "lora_scale": 1.0,
    "steps": 8,
    "sampler": "euler",
    "scheduler": "simple",
    "shift_video": 12.0,
    "shift_audio": 3.0
  }
}
```

All selections and settings must validate together. A missing component or
incompatible mode rejects the entire preset and leaves standard defaults in
place. The inventory endpoint exposes resolved defaults to both web and native
iOS clients; a fresh iOS workflow screen selects the installed preset without
needing a new IPA.

The web client applies the preset's sampling settings and LoRA once per installed
preset revision to a Reference-mode draft. It preserves the prompt, attachments,
base components, dimensions, duration, seed, and GPU choices. Later manual
changes remain saved. A draft in another mode keeps its settings and offers
**Use installed preset** to switch explicitly to the configured model and
mode. Queued-job editing and workflow import do not automatically apply presets.
Running or queued jobs are never modified by installation.

`ref2v`/`ref2va` LoRAs require the Reference model; `fl2v`/`fl2va` LoRAs require
the First/last-frame model (also used in text-only mode). The server rejects
cross-family selections, including those submitted by an older native client.

The LightX2V Ref2V 8-step v1.0 preset uses the
[author's Ref2V settings](https://huggingface.co/lightx2v/Minimax-h3-Turbo/discussions/51):
8 steps, Euler, video shift 12, and audio shift 3, with Simple scheduling and
LoRA strength 1. On NVFP4 transformers, the worker applies the adapter through
the inference core's bypass loader so its residual is not rounded into the
quantized base weights. This needs additional adapter VRAM and computation;
fewer sampling steps alone do not establish a measured end-to-end speedup.

The workflow's **GPU memory** panel shows live VRAM used/total and utilization
for both cards, refreshing every two seconds while the panel is visible. These
readings include other applications using the GPUs. A failed refresh is marked
unavailable, with the time of the last reading.

**Model / encoder GPU** selects the main inference device. **VAE GPU** separately
selects the device for video/audio VAE encoding and decoding. For this 5090/4090
setup, a new draft or an older saved draft without a VAE setting selects the
4090 for VAEs when the model GPU is the 5090. Explicit saved choices, including
**Same as model GPU**, are preserved. NVFP4 model/encoder work remains on the
compatible main GPU; selecting the 4090 for VAEs does not move those weights.
Placement labels describe the current draft. Changing them affects new jobs;
it does not move an already queued or running job between GPUs.

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

### Multiple LoRAs

Use **Add LoRA** to build an ordered stack of up to eight H3 adapters, each with
its own strength from −4 to 4. A strength of 0 keeps the selection but disables
that adapter. **Remove** removes one selection; removing all rows uses the base
model alone. An adapter can appear only once. Active adapters must match the
selected FL2VA or Ref2VA mode; shared adapters work with either. Stacking is
supported by the runtime, but different speed and editing adapters can need
different steps, samplers and strengths; combining them does not guarantee the
same result as using either one alone.

Drafts, batch jobs, queued edits and reruns keep the entire ordered stack.
Refreshing components preserves missing selections and blocks generation until
their files return or the rows are removed. The queue parameter editor offers
**LoRA stack (replace all)** to change the full stack on the checked jobs. Leave
that parameter unchecked to preserve each job's original adapters.

The API accepts `loras: [{"id": "component-id", "strength": 1.0}]`. An explicit
empty array disables all adapters; when `loras` is absent, older `lora` and
`lora_scale` settings migrate to one row. Workflow ZIPs remain version 1 and
store ordered `lora_0`, `lora_1`, … component references with corresponding
`config.lora_strengths`. Exports with weights include every selected adapter.
Older single-adapter exports still import. Unavailable imported adapters stay
visible for repair instead of being silently dropped.

### VFX Edit LoRA

Select the Ref2VA model and
`minimax_h3_vfx_edit_v1.0_r128.safetensors` in any **LoRA** row, at a nonzero strength, to use
VFX Edit. This is a standard adapter: it does not use the BFS workflow or require
another UI. The source video becomes an aligned guide for the edit, and its
original soundtrack is retained when present. A silent source stays silent.

Choose one **Source video** and write a concise instruction describing the change,
such as "Add glowing blue energy around the dancer's hands." The server adds
`vfx_edit:` once when submitting; selecting the adapter preserves your draft
prompt. **Enhance prompt** uses guidance for concise edits. The enhancement model
does not receive the video itself, so describe the requested change in your text.

VFX Edit accepts no first/last frames, reference images, or separate audio files.
Inputs already in the draft remain available with **Remove** buttons and block
submission until removed, or until you select another adapter. Multiple source
videos can be queued with **Batch job**, one source per job. Existing queued jobs
and their captured settings do not change when the draft's LoRA changes.

Output duration follows the source at 24 fps, with a minimum of 73 frames.
The worker pads the guide to the next `17k + 5` frame count, then trims that
padding from the output so the source ending and soundtrack are retained.
The VFX frame values include 73, 90, and 107 in
addition to the normal range; the duration control is disabled because the worker
matches each source automatically. The source aspect ratio is retained within the
chosen **Width × Height** pixel budget. The usual Euler/Simple sampling controls
remain available; start with 20 steps and LoRA strength 1.

## Optional prompt enhancement

Write your prompt, select the video mode and duration, then choose a configured
chat model in **Enhancement model**. The initial default is the loaded local
Qwen3.8 27B when available. Your explicit endpoint/model choice is remembered in
this browser for your account. **Check models** refreshes the available choices.
If your remembered model is no longer listed, enhancement stays disabled until
you choose an available model. Connection errors preserve your draft; enhancement
does not silently switch providers or models.

Click **Enhance prompt** to send your prompt and applicable uploaded images to
your selected model. A cloud model receives that text and those images through
its configured provider. Enhancement does not start a video render. Review and
edit the rewritten prompt, or use **Restore
original** to return to the prompt you supplied, before choosing **Generate video**
yourself. You can also generate directly from your own prompt without enhancement.

The rewrite organizes your description for the selected H3 mode, including
actions, camera movement, timing and sound. It preserves your requested dialogue,
visible text and reference labels. Its guidance follows MiniMax's official
[text/keyframe guide](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/base-en.txt)
and [reference-mode guide](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/ref-en.txt).

The model is first told that it is rewriting a prompt for an H3 video render.
With an image-capable enhancement model, first and last frames are labeled as
the opening state and final target, with instructions to describe a plausible
transition. A last-frame-only input is labeled `<Picture 1>`. Reference images
are sent in their upload order and guide appearance or other requested roles;
they are not automatically treated as first or last frames.

The model can use the images' visible details when rewriting. State each
reference's intended role in
the **Prompt**, such as which image supplies identity, clothing, composition or
the ending pose; an image's contents alone do not establish your intent. For
example, specify that `<Picture 1>` supplies identity and `<Picture 2>` supplies
only clothing when that is what you want. Review the rewrite before rendering.

Video and audio contents are not sent to the enhancement model: it receives
their counts and any descriptions you put in the prompt. Describe motion,
dialogue or sound from those references yourself. Their counts and labels are
not evidence of their contents. Enhancement does not silently switch models
when the selected model cannot process images. This prompt rewrite is not
MiniMax's official H3-Context-IR service and does not claim equivalent multimodal
understanding or output quality.

Enhancement leaves any local chat model server running. If that model and H3
share a GPU and H3 needs more memory, you can stop the chat model through Cookbook
after enhancing and before generating. Odysseus does not automatically stop that
server for enhancement.

## Controls

- Width/height: multiples of 32; area at most `768 × 1344` pixels.
- Frames: aligned upward to `17k + 5`, at 24 fps. The default 124 frames is about
  5.17 seconds; 362 frames is about 15.08 seconds. The low-level worker accepts
  shorter smoke-test clips, but H3's documented trained range starts at 124.
  VFX Edit instead matches the source duration and supports at least 73 frames.
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
this path does not use negative conditioning. The standard H3 editor does not
expose Fun Control patches or arbitrary per-frame guide tracks.

For BFS head swapping, open **Cookbook → Launch → BFS Video · Local**. Its
dedicated H3 recipe combines a head identity image with a source-video guide
and the matching BFS LoRA. It also provides recipes for LTX-2, LTX-2.3,
LTX-2.5 and Wan 2.2. See [BFS video workflows](bfs-video.md) for required media,
component files and the supported generation paths.

The worker exits after each finished or failed job, releasing its CUDA context.
Stopping a job terminates its worker process; it does not unload unrelated model
servers. Cold starts include model loading and prompt encoding. Progress reports
those phases separately from denoising steps, video/audio decode and MP4 encode.

Use **Generate video** for the first submission, then **Add to queue** while jobs
are active. Edit the prompt, controls, or attachments between submissions. Each
accepted job saves its own prompt, settings, and uploaded files; further draft
edits do not change it. The draft stays available so you can submit another
variation without reselecting every file.

H3 and BFS share a queue and render one job at a time in submission order.
Queued cards show their position in that shared queue and **Cancel queued job**;
running cards show rendering progress and **Stop**. Rendering time starts when
the worker starts, separately from time spent waiting. Closing the panel does
not stop the queue. Refreshing or reopening it restores the saved job list.

Each entry in **Recent video jobs** has a **Prompt used** section with a **Copy prompt**
button. It shows the prompt submitted for that render, including enhancement and
any edits you made before submitting the job. The prompt is saved with
the job, so it remains available after a page reload or server restart. Existing
jobs also show their saved prompt when it is present in their job manifest.
Unsent drafts and earlier versions of an enhanced prompt are not part of this
per-job history.

Completed, failed and stopped cards offer **Delete job**. The in-panel
confirmation lists what will be removed: the job, its uploaded media, logs,
saved prompt and output, including its linked Gallery record/output. Choose
**Delete permanently** to confirm, or **Keep job** to leave it intact. Running
and queued jobs must be stopped or canceled first. A deletion error leaves the
card visible so you can retry; deleting a job does not change your current draft.

## Queuing a batch of videos

In **References → video + audio**, check **Batch job**, drop or select all the
videos, and click **Queue N videos**. Each file becomes its own job, with the same
prompt, model, LoRA, GPU, seed, render settings, and shared image/audio inputs
captured at the moment you queue the batch. Videos upload individually in the
displayed filename order; generation uses the existing shared FIFO queue.

With **Batch job** off, the ordinary reference-video control still puts multiple
videos in one job; VFX Edit requires exactly one source video. Its selected files
are preserved while batch mode hides it;
they are never silently included in batch jobs.

The batch list shows each filename and upload/queue result. **Pause adding**
finishes the current upload before pausing; **Retry failed** reuses each file's
original submission ID and settings, so a lost response cannot duplicate a job.
Closing and reopening the panel preserves the list and pauses pending uploads.
Resume uses the captured settings even if the reopened draft is empty. A page
refresh loses files not yet queued; confirmed server jobs continue independently.
**Clear list** clears the local list without deleting any server jobs. Remove
and re-add an unqueued file to use changed settings instead of its saved snapshot.

## Moving workflows to another installation

**Export workflow** above the editor exports the current draft. Each saved job
also has **Export workflow**, which uses that job's saved configuration. The
export dialog offers **Model references only** for a small portable workflow,
or **Include model weight files** to bundle the selected transformer, encoder,
VAEs and LoRAs. **Include input images, video and audio** is a separate option.
Both weight files and input media are excluded by default. Weight bundles can
be very large; they stream directly from the server without buffering the whole
archive in browser memory. In Chrome or Edge on HTTPS (or localhost), choose
**Choose location & export** to pick the destination folder and filename first.
The dialog shows bytes saved and confirms **Saved** only when writing finishes.
Keep the panel open; **Cancel export**, Escape, or closing the panel cancels an
in-progress save. A failed or canceled transfer does not commit a partial archive.
The browser may leave an empty file if the picker created a new destination.

**Browser download** hands the file to the browser's download manager instead.
This is the fallback for browsers without a save picker, including Safari.
Its destination follows the browser's download settings; Odysseus reports the
handoff and cannot confirm the final location or completion. The server does
not keep a completed export ZIP in its cache or job folders.

Open **Import workflow** in the matching H3 panel on the destination Odysseus
installation. Bundled weights are installed in its private model cache and
matched to the imported selections. The panel shows upload progress followed
by the file-checking/installing phase. Reference-only exports need matching
locally available components; unmatched or ambiguous files remain unselected
with an explanation. Imports retain the destination's model and VAE GPU choices.
Included input media is restored; an export without media clears previous draft
attachments so unrelated inputs are not reused. Importing restores a draft and
never submits a generation job. Review the models, settings and inputs before
choosing **Generate video**.

If you edit the draft while an import is processing, the imported settings do
not overwrite those edits. Any weights already imported into the cache remain
available for a later import or manual selection.

## Worker contract and validation

The backend writes a private job manifest and launches:

```bash
venv/bin/python scripts/h3_video_worker.py --job /absolute/path/manifest.json
```

The manifest has `config`, `uploads`, `runtime_path`, `output_path` and
`status_path`. Config includes the four absolute component paths, selected GPU,
optional `vae_gpu` (empty means the main GPU),
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
