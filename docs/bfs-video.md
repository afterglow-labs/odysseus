# BFS video workflows in Odysseus

Open **Cookbook → Launch → BFS Video · Local**, or launch the cached
`Alissonerdx/BFS-Best-Face-Swap-Video` repository. **BFS Video Workflows** runs
head swapping on the local Odysseus server. Choose a recipe, its cached model
components and GPU, upload the required media, then click **Generate video**.
From a cached BFS repository, select the adapter and workflow, then click
**Choose face/head and target video** to open the input form. That button does
not start rendering; **Generate video** submits the job after you add the inputs.

The seven recipes adapt the author's generation and guide-conditioning methods.
They generate an MP4 at the selected output size in a single generation pass.
The author's optional first-frame image editors, captioning models, previews,
comparison layouts and upscale/refinement passes are separate operations and
are not executed here. Odysseus does not import arbitrary workflow JSON or open
a ComfyUI window.

## Queue controls and rerunning jobs

The **Queue controls** section above recent jobs provides **Pause queue** /
**Resume queue**, **Cancel queued jobs**, **Delete queued jobs**,
**Edit queued parameters**, and **Run again with new parameters**. Pause is
shared with your H3 jobs and allows the current render to finish. Cancel keeps
records and inputs; delete permanently removes the waiting jobs after confirmation.
Cancel/delete can target just BFS or all your H3 and BFS queued jobs.

Bulk editing changes only checked parameters and retains each job's workflow,
inputs and queue position. Parameters and components must be valid for every
selected workflow. Rerunning creates new copies of finished, queued or all BFS
jobs with the checked changes, preserving original records and results. Review
the job count before confirming; paused queues remain paused after adding copies.

To change just one finished job, choose **Edit & rerun** on its card. Its own
saved parameters populate the editor; submitting creates one new copy with the
same saved media and your changes. The original result and new-job draft stay
intact. **Retry** for failed or stopped jobs and **Reprocess** for completed jobs
reuse the saved parameters immediately. **Retry request** recovers an interrupted
submission using its original request ID and parameters, avoiding duplicate copies.

## Editing queued jobs

Use **Edit** on a queued job to change its prompt, workflow, components, render
settings, or saved face/head and target inputs. **Save changes** updates the same
job without moving it to the back of the queue or reuploading retained media.
**Cancel edit** restores your new-job draft. If the job starts or another client
updates it first, saving is rejected and the unsaved edit stays visible. Running
and finished jobs cannot be edited in place. Finished jobs support **Edit &
rerun** to create a new copy with changed parameters.

## Recipes and required inputs

Every recipe needs a source video and an identity input. The meaning of that
identity input depends on the recipe:

| Recipe | Identity and additional inputs | Conditioning used |
| --- | --- | --- |
| MiniMax H3 · Head swap | Head reference photo | Ref2VA receives the head image; `MiniMaxH3AddGuide` anchors the source frames and selected soundtrack at frame zero. |
| LTX-2 · First-frame head swap | An already head-swapped first frame; optionally a prepared last frame with the matching first-and-last-frame LoRA | Pins the prepared keyframe(s), then adds the original source-video guide. |
| LTX-2 · Masked head swap v2 | Clean face photo or prepared first frame, plus a matching head-mask video | Pins the supplied image and builds the required magenta guide from the source and mask. |
| LTX-2.3 · Persistent identity v3 | Head reference photo | Composes the author's persistent green identity panel into every guide frame before encoding the guide. |
| LTX-2.5 · Head swap v1 | Head reference photo | Uses the author's overlapping guide-video and identity conditioning with separate source IDs. |
| LTX-2.5 · Head swap v1.1 | Head reference photo | Uses the same overlap path with the v1.1 adapter and its reference-guidance setting; default head-swap strength is 0.8. |
| Wan 2.2 · Bernini head swap | Head reference photo | Encodes source-video and identity context latents, then samples with the Bernini-R high-noise and low-noise models and their matching BFS LoRAs. |

The [author's repository](https://huggingface.co/Alissonerdx/BFS-Best-Face-Swap-Video)
contains the adapters and original workflows. The H3 recipe needs no head mask
or Fun ControlNet patch. Its source video is a guide, not an ordinary Ref2VA
reference-video input.

For LTX-2 v1, prepare the head-swapped first frame before uploading it. Odysseus
does not run the author's optional FLUX image-editing stage. Select
`head_swap_v1_13500_first_frame.safetensors` for a first frame alone. Select
`head_swap_v1_8750_first_and_last_frame.safetensors` when supplying both prepared
keyframes; that adapter requires the last frame.

For LTX-2 v2, white in the mask must cover the entire original head, including
hair, and black must preserve the rest of the scene. The mask video must align
with the source video, including the selected start time, and cover the entire
selected segment. The worker converts the white region to magenta internally.
Its image input is pinned as the opening frame. A clean face photo is supported;
the author recommends a prepared first frame to establish the correct opening
pose, lighting and composition. Masking removes the original head's facial
micro-movements from the guide, so this differs from the persistent reference
conditioning used by the newer recipes.

## Setup and environment

Run setup from the WSL checkout with Odysseus's private Python:

```bash
./venv/bin/python scripts/setup_bfs_runtime.py
```

Setup prepares the existing Comfy inference core at commit
`5c460d8172fe30761ff67c0df3d5643bb74e0d70` and the selected, unmodified
[BFSNodes 1.21.2 helpers](https://github.com/alisson-anjos/ComfyUI-BFSNodes/tree/3fb1155e11f178dadd2743e74349c3359b7c57dc)
at revision `3fb1155e11f178dadd2743e74349c3359b7c57dc`. The source manifest
and checksums are in `scripts/bfs_runtime_sources.json`. The LTX-2.3 panel and
LTX-2.5 overlap implementations use these helpers directly; setup does not load
the upstream custom-node registration or start a ComfyUI server.

All Python dependencies stay in the selected private environment. The runtime
sources live under `runtimes/minimax-h3/`; upstream licenses remain alongside
them. Setup refuses global Python, preserves differing existing source files,
and does not download model weights. `--skip-dependencies` verifies/prepares the
source without installing Python packages.

The recipes accept compatible Comfy-format `.safetensors` components. A BFS
LoRA augments its matching base transformer; the BFS repository alone is not a
complete runnable model. These workers do not load GGUF transformers or arbitrary
Diffusers repositories. NVFP4 components require the RTX 5090/another supported
Blackwell GPU. Choose the displayed physical GPU; WSL's numeric GPU orders can
differ between CUDA and `nvidia-smi`.

## Model components and missing files

Component selectors show compatible files found in Cookbook's local model
folders and Hugging Face caches. Keep the original component filenames so the
scanner can recognize their family and role. Every required selector must have
a valid file before the workflow can run.

H3 requires a Ref2VA transformer, its Qwen3-VL H3 encoder, both H3 VAEs and
`h3/minimax_h3_head_swap_v1.0_r32.safetensors`. The speed LoRA is optional and is
applied before the head-swap LoRA. See the
[H3 component documentation](minimax-h3.md#environment-and-model-files).

LTX-2.5 v1 and v1.1 use the distilled transformer, Gemma4 encoder with its
projection included, and matching video/audio VAEs from
[Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5).
Choose the BFS v1 or v1.1 LoRA matching the selected recipe. Rank 64 and rank 128
variants are alternatives, not additional mandatory files.

The local-folder audit on 2026-10-06 found H3 and LTX-2.5 component sets and the
BFS adapters. It did not find the following LTX-2/LTX-2.3 components. These exact
filenames were verified against the publishers' Hugging Face metadata; no
weights were downloaded during the audit.

| Recipe family | Required missing component | Verified source |
| --- | --- | --- |
| LTX-2 v1/v2 | `ltx-2-19b-dev-fp8_transformer_only.safetensors` | [Kijai transformer](https://huggingface.co/Kijai/LTXV2_comfy/blob/main/diffusion_models/ltx-2-19b-dev-fp8_transformer_only.safetensors) |
| LTX-2 v1/v2 | `ltx-2-19b-embeddings_connector_dev_bf16.safetensors` | [Matching text connector](https://huggingface.co/Kijai/LTXV2_comfy/blob/main/text_encoders/ltx-2-19b-embeddings_connector_dev_bf16.safetensors) |
| LTX-2 v1/v2 | `LTX2_video_vae_bf16.safetensors` | [Video VAE](https://huggingface.co/Kijai/LTXV2_comfy/blob/main/VAE/LTX2_video_vae_bf16.safetensors) |
| LTX-2 v1/v2 | `LTX2_audio_vae_bf16.safetensors` | [Audio VAE](https://huggingface.co/Kijai/LTXV2_comfy/blob/main/VAE/LTX2_audio_vae_bf16.safetensors) |
| LTX-2 and LTX-2.3 | `gemma_3_12B_it_fp8_scaled.safetensors` | [Comfy-Org Gemma3 encoder](https://huggingface.co/Comfy-Org/ltx-2/blob/main/split_files/text_encoders/gemma_3_12B_it_fp8_scaled.safetensors) |
| LTX-2.3 v3 | `ltx-2.3-22b-distilled_transformer_only_fp8_input_scaled_v3.safetensors` | [Kijai distilled transformer](https://huggingface.co/Kijai/LTX2.3_comfy/blob/main/diffusion_models/ltx-2.3-22b-distilled_transformer_only_fp8_input_scaled_v3.safetensors) |
| LTX-2.3 v3 | `ltx-2.3_text_projection_bf16.safetensors` | [Matching text projection](https://huggingface.co/Kijai/LTX2.3_comfy/blob/main/text_encoders/ltx-2.3_text_projection_bf16.safetensors) |
| LTX-2.3 v3 | `LTX23_video_vae_bf16.safetensors` | [Video VAE](https://huggingface.co/Kijai/LTX2.3_comfy/blob/main/vae/LTX23_video_vae_bf16.safetensors) |
| LTX-2.3 v3 | `LTX23_audio_vae_bf16.safetensors` | [Audio VAE](https://huggingface.co/Kijai/LTX2.3_comfy/blob/main/vae/LTX23_audio_vae_bf16.safetensors) |

Wan 2.2 needs both Bernini-R transformers, not an ordinary Wan image-to-video
checkpoint. Both were missing in the same audit:

- [`wan2.2_bernini_r_high_noise_fp8_scaled.safetensors`](https://huggingface.co/Comfy-Org/Bernini-R/blob/main/diffusion_models/wan2.2_bernini_r_high_noise_fp8_scaled.safetensors)
- [`wan2.2_bernini_r_low_noise_fp8_scaled.safetensors`](https://huggingface.co/Comfy-Org/Bernini-R/blob/main/diffusion_models/wan2.2_bernini_r_low_noise_fp8_scaled.safetensors)

Its UMT5-XXL encoder and Wan 2.1 VAE were already present at:

```text
/mnt/e/AI/ComfyUI-Studio/models/text_encoders/umt5_xxl_fp16.safetensors
/mnt/e/AI/ComfyUI-Studio/models/vae/wan_2.1_vae.safetensors
```

The two BFS Wan adapters are
`wan22/headswap_bernini_r64_73f640_step3000_high_noise.safetensors` and
`wan22/headswap_bernini_r64_73f640_step3000_low_noise.safetensors`. Both are needed.

In Cookbook's local server settings, add the folder containing downloaded
components to the model directories and save. For example, the existing Windows
model tree is `/mnt/e/AI/ComfyUI-Studio/models` from WSL. Return to **BFS Video
Workflows** and click **Refresh components**, then select the matching files.
Use paths visible to the Linux server, not Windows drive-letter paths. The
panel runs on the local Odysseus server; selecting a remote SSH server elsewhere
in Cookbook does not move BFS inference there.

## Controls, output and stopping

Output is H.264/AAC MP4 at 24 fps. The source-start control selects the beginning
of the input segment. **Maximum frames from source** is capped by the available
source frames and trimmed downward to the family's frame grid: `17k + 5` for H3,
`8k + 1` for LTX, and `4k + 1` for Wan. The output therefore may be shorter than
the requested maximum. H3's documented trained range starts at 124 frames;
shorter settings are principally useful for functional checks.

Choose **Fit whole frame** to preserve the complete source with padding, or
**Crop to fill** to fill the selected canvas. Width and height are multiples of
32; the current total area cap is 1,032,192 pixels. A higher resolution or longer
clip increases inference cost. The output is generated at this size; no automatic
second-pass upscaling is applied.

The panel exposes seed, steps, head-swap strength and source/silent audio. H3
also exposes sampler, scheduler, separate video/audio shifts, identity detail
and optional speed-LoRA strength. LTX/Wan expose guidance. Defaults are 20 steps
for H3 and LTX-2; 8 for distilled LTX-2.3/LTX-2.5; and 40 steps with CFG 5
for Wan’s full-quality branch. H3 uses CFG 1. The
LTX-2.5 v1.1 strength default is 0.8. Keep source audio preserves the selected
source segment's timing; silent mode writes a silent soundtrack. Generated
model audio is not substituted for the source soundtrack.

The distilled LTX recipes use the standard eight-step distilled sigma schedule
at their default step count and the core's simple schedule at other counts.
This does not reproduce the custom `bong_tangent` scheduler in the author's
LTX-2.3 workflow; identical output to that workflow is not implied.

Use **Generate video** for the first job and **Add to queue** while jobs are
active. Each submission saves its own prompt, settings, and uploaded media;
the draft remains available to edit for your next job. H3 and BFS jobs share
one queue and render one at a time in submission order. Queued cards show
their position and **Cancel queued job**; they do not report rendering
progress until their worker starts.

The queue continues if the panel closes. Progress, logs, playback and the saved
**Prompt used** are available in **Recent BFS jobs**; completed videos also go
to Gallery. The stored prompt includes the recipe's identity/source-role
instructions and any notes supplied before generation. Use **Stop** to terminate
the owned render process and release its GPU allocations. A job remains active
if shutdown cannot be confirmed. A shared render lock prevents H3 and BFS from
launching competing video renders at the same time. Unrelated chat
model servers are not stopped automatically.

Completed, failed and stopped cards offer **Delete job**. The confirmation
removes that job's uploaded media, logs, saved prompt and output, including its
linked Gallery record/output. **Delete permanently** confirms the removal;
**Keep job** cancels it. Running and queued jobs must be stopped or canceled
first. Errors leave the card available for retry, and deleting a job does not
change the current workflow draft.

## Queuing a batch of target videos

Check **Batch job**, drag all the target videos into the batch area, and click
**Queue N videos**. Each becomes one job with the same captured prompt, workflow,
models, seed, and render settings. Identity images and any selected last-frame or
mask input are shared across the batch. Choose inputs appropriate for every
target; the runtime still validates each video. Uploads proceed one at a time in
the displayed filename order, then render through the existing shared FIFO queue.

The ordinary target-video selection is preserved while batch mode hides it.
Turning **Batch job** off restores that single-job input. The batch list shows
per-file progress and failures. **Pause adding** finishes the current upload;
**Retry failed** keeps original settings and a stable submission ID to prevent
duplicate jobs after a lost response. Closing and reopening the panel retains
pending files and lets you resume. Refreshing the page loses unqueued files;
already queued jobs continue on the server. **Clear list** does not delete them.

## Exporting and importing workflows

Use **Export workflow** above the editor for the current draft, or on a saved
job for that job's settings. The export dialog lets you choose **Model references
only** or **Include model weight files**, and independently include the input
images, video and audio. References-only and no input media are the defaults.
Weight bundles include the selected model components and may be very large;
they stream directly from the server. In Chrome or Edge on HTTPS (or localhost),
**Choose location & export** opens a folder/filename picker before downloading.
Keep the dialog open to see bytes saved and the final **Saved** confirmation.
Cancel, Escape, or closing the panel stops an in-progress save without committing
a partial archive; a newly selected file may remain empty after cancellation.
**Browser download** uses the browser's download folder and progress instead;
this is the fallback for Safari and other browsers without the save picker.
Odysseus cannot confirm completion or the destination in that mode. No completed
ZIP is retained in the server's cache or job folders.

Use **Import workflow** in the destination installation's BFS panel. Bundled
weights go into its private model cache; matching selections are restored from
the resulting inventory. Upload progress and the subsequent checking/installing
phase appear in the panel. Missing or ambiguous components remain unselected
with an explanation. The destination retains its local GPU selection. Included
media is restored, while imports without media clear previous draft inputs.
Importing never generates a video automatically. Review the selected recipe,
models and inputs, then generate when ready. Edits made while an import is
processing are preserved instead of being overwritten by its result.

## Validation

Run the CPU tests without loading model weights:

```bash
./venv/bin/python -m pytest -q \
  tests/test_bfs_video.py tests/test_bfs_h3.py tests/test_bfs_ltx_wan.py
```

The tests cover recipe-specific guide construction, adapter ordering and
compatibility, component selection, owner-scoped access, upload limits,
cancellation, shared render locking, frame alignment, video resampling,
source-audio synchronization and output dispatch. A small CPU forward check
also exercised the pinned LTX overlap helper against the pinned inference core.

Real BFS GPU smoke tests completed on 2026-10-06 through isolated API,
supervisor and worker instances with a separate database and synthetic inputs:

- H3 on RTX 5090: cached Ref2VA NVFP4 transformer, H3 encoder/VAEs and BFS
  head-swap LoRA; 5 frames at 256×256, two steps.
- LTX-2.5 v1 on RTX 4090: cached INT8 transformer and Gemma4 encoder, matching
  VAEs and BFS rank-64 LoRA; 17 frames at 256×256, two steps.
- LTX-2.5 v1.1 on RTX 4090: matching rank-64 v1.1 LoRA, eight-step distilled
  schedule and reference guidance 1.3; 17 frames at 256×256, with the non-silent
  source soundtrack preserved as stereo 32 kHz audio.

All three produced playable MP4s with the expected frame count, owner-scoped delivery,
Gallery persistence and the exact submitted prompt. These are functional smoke
tests, not evidence of production face-swap quality at those reduced settings.
LTX-2/LTX-2.3 and Wan base weights were missing; their recipe and actual core
conditioning paths were checked on CPU without claiming completed GPU renders.
