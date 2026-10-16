# H3 Seed Hunter

Odysseus's native port of **MiniMax H3 Seed Hunter Latent v2.5.1** by
foxydits / FoxFurEssence Films. Open **Cookbook → Launch → H3 Seed Hunter**
on desktop, or the separate **H3 Seed Hunter** workflow in the iOS client.
It uses the existing private Comfy inference core without a ComfyUI window or
server. It is an implemented recipe, not an arbitrary Comfy graph executor.

The classic editor remains available. **Cookbook → Launch → Build Pipeline** opens the separate [pipeline workspace](workflow-pipeline.md) using the same native controls and execution path. Node editing remains experimental.

## Preview, choose, refine

1. Choose a Ref2VA checkpoint, encoder, VAEs and adapters. The source workflow's
   instance selects Singularity Ref2VA; its internal FL2VA loader default is
   overridden. Other compatible Ref2VA weights remain selectable.
2. Generate one to three previews. They share the prompt and settings and use
   the base seed, base + 1000, and base + 2000. The model loads once per job.
3. Play the results and select a candidate. Selection only opens the refinement
   editor; it does not enqueue anything.
4. Choose the final length, dimensions, steps, sampler, scheduler, denoise and
   LoRAs, then submit the final render explicitly. Reference-video duration
   matching applies to this final target, independently of the short previews.

Preview duration is the duration of **each** candidate; selecting three
candidates does not triple it. The seconds control applies immediately and
shows the supported frame length actually used (for example, requesting
4 seconds selects 107 frames, about 4.46 seconds).

Saved jobs are paged in groups of 10 on desktop and iOS. Candidate videos load
when played. Page changes do not change a selected refinement or the rendering
queue; refresh the list to return to the newest jobs. An opened iOS job remains
available independently of the current results page.

**Delete all job history**, beside **Refresh jobs**, removes finished job
records and their saved inputs, candidate previews, latents, final videos and
Gallery entries after reviewing and confirming the count. Its scope covers
every page of this workflow, or all your H3 and BFS workflows when selected.
Running and queued jobs are preserved. Progress and individual errors remain
visible, and a retry uses the same reviewed selection.

Original input videos are playable directly above the generated previews, in
both classic and Pipeline job cards and the iPhone job detail. Each player
shows its filename and reference number; multiple videos remain separate and
in attachment order. A saved reference section plays that section and labels
its start and duration. Otherwise playback covers the whole original video,
independently of the short generated-preview length.

Media loads only after **Play original video**. Viewing history or refreshing
status does not download or convert source videos, select a seed, or queue a
render. The server prepares a cached, browser-compatible H.264/AAC copy when
needed; the stored original and generation inputs remain unchanged. Source
playback is available only to the job's owner and stops when its player closes.

Defaults follow the saved graph: 0.4 MP at 9:16 (480 × 864), 73 frames at 24 fps,
6 steps, ER SDE with beta scheduling. Refinement defaults to about 2 MP at the
preview's actual aspect ratio (1088 × 1952), 2 steps, ER SDE,
linear-quadratic scheduling and 0.4 denoise. Video/audio sigma shifts are
bypassed until explicitly enabled.

Fresh drafts use 20 preview steps and no automatic turbo LoRA. Previously saved drafts and imported workflows retain their explicit settings; the 6-step values above describe the original author's turbo-based preset.

## Seed Scout

**Cookbook → Launch → H3 Seed Scout** is a separate native workflow. It samples several consecutive seeds through the first part of one full schedule and shows animated previews inside Odysseus. **Continue this seed** explicitly resumes the selected candidate's exact partial latent through the remaining schedule, at the same resolution. Viewing a preview, refreshing status, or closing the client never chooses a seed.

Fresh Scout drafts use 20 full-schedule steps, 3 seed choices, previews after step 3, VAE decoding, and no LoRAs. Selection timeout 0 waits until a seed is chosen or the job is stopped. Optional previews-only and single-full-pass modes remain available. Batch submissions can vary a reference video, image or audio input, or repeat the whole draft; video lengths can be detected automatically. Scout drafts, batch trays and paginated history are separate from Seed Hunter.

Seed Hunter's completed-preview/upscale workflow and Seed Scout's partial-schedule continuation are distinct operations.

The preview's denoised video/audio latent and original conditioning are stored
as a safetensors file plus typed JSON. When final and preview lengths match,
refinement uses that exact saved latent, applies the learned 3D spatial upscaler
to video and retains the audio latent. When lengths differ, the final job first
generates a candidate at the target duration and preview resolution using the
selected seed, original preview sampling settings and saved inputs. It then
upscales and refines that full-length latent. A longer generation can differ
from the short audition; this is not an exact continuation of its frames.
Each child job owns its snapshot and inputs, so deleting a parent cannot
break a queued child. Final jobs retain the original prompt, encoder, model
and codecs; length, rendering parameters and LoRAs remain editable.

## Other branches

- **Single pass:** one full render without preview selection.
- **Continuation:** trim a source video if needed, retain its prefix and use its
  last 5, 22, 39 or 56 frames as a guide. The overlap is blended at 0.5 as in the
  source graph. Output length is source frames + generated frames − overlap.
- **Dialogue:** assemble a visible dialogue prompt and optionally provide a
  trimmed voice reference. Produces playable compatibility MP4s and downloadable
  FLAC candidates. This is generation, not a guarantee of exact voice identity.
- **First/last guides:** dedicated guide images or Picture 1 / Picture 2 can
  guide the boundary frames while normal reference inputs remain available.
- **Preserve supplied audio:** Audio 1 is encoded into the audio latent with a
  zero noise mask, rather than used only as a loose sound reference.
- **RefMods:** up to eight encoded reference safetensors bundles, with strengths
  from 0 to 1. These are reference latents, separate from the LoRA stack.
- **Reference video sections:** attach a longer video, preview and scrub it,
  then choose a start and a 2–15 second length for each reference. Video and audio
  use the same section; ranges survive queued edits, reruns and portable workflows.
  Multiple video sections still share the 15-second reference budget. Choose a
  generation length at least as long as the selected section to use all of it:
  H3 caps reference conditioning to the generation's frame count. Refinements
  retain their preview's original selection with its saved conditioning.
- **48 fps:** learned RIFE interpolation preserves the 24 fps source duration
  and remuxes its audio. It is not simple frame duplication.
- Optional sparse attention and KJ feed-forward/head chunking controls mirror
  the supplied graph. They are off by default and exposed only when installed.

All branches share Odysseus's existing server queue with H3/BFS. Drafts, model
selections and queue editing are separate from the regular H3 workflow.
Preview workflows can be exported with optional weights and attachments.
Export the source preview workflow rather than a final job: the portable
workflow format does not currently transfer generated latent snapshots.

## Batch jobs

Turn on **Batch job** and choose the input that changes between jobs. Preview
and single-pass batches can vary a reference video, image or audio file, or
repeat the whole draft. Continuation batches use one source video per job;
dialogue batches use one voice reference per job. Other inputs, the prompt,
LoRAs and settings are captured separately for each submission. Repeated
copies keep the chosen seed. Batch inputs do not replace the ordinary controls
for combining several references in a single job.

Expand **Inputs for this job** under a batch row to add that video's own audio,
images or additional reference videos. These remain attachments of that row;
they do not create separate jobs. Each role uses that row's files when supplied,
otherwise the main draft's explicitly shared references. Removing the last row
attachment restores the shared references for that role. Audio and image inputs
can use local files or the E:/F: server browser, in both classic and pipeline
layouts and the native iOS client. Input requirements are checked for every row
before the first submission, and retries keep each row's captured inputs.

The Hunter **Preview count** picker offers 1, 2 or 3 candidates per input;
older draft/import values outside that range are capped when opened. Scout's
separate seed-choice limit is unchanged. Reference audio supports Matroska
audio (`.mka`) as well as WAV, MP3, FLAC, M4A, AAC, OGG and Opus.

Reference video lengths are detected automatically by default. In Hunter
Preview mode this selects the **final** output length and leaves every
candidate at the explicitly chosen preview length. Single-pass batches match
their generation length to the source as before. Each row shows its source
duration and supported target length; **Choose a section** enables optional
manual trimming. Preview and final lengths use the Seed Hunter 73–362 frame
range independently. Continuation keeps the configured additional generation
length and overlap instead of matching the duration of its source. Dialogue
keeps its configured generation length. Final rendering remains tied to one
selected preview, its seed and its saved inputs.

Server jobs keep rendering while more batches are added. Each batch retains
its own prompt and settings, so editing a new draft does not alter accepted
jobs. The server preserves submission order across H3, Seed Hunter and BFS.

The question-mark controls explain each option on desktop; the native client
uses information buttons. Sampler and scheduler help describes the selected
choice, including Simple, Normal, Beta and Linear quadratic. Both clients use
the same bundled catalogue in `static/seed-hunter-help.json`.

Feedforward chunks set to zero use automatic temporary-memory protection for
INT8 models. A positive count selects the explicit chunking control (one means
no splitting). Automatic protection leaves other quantization layouts alone
and does not reduce the requested dimensions, frames or steps.

## Private installation

With the H3 inference core already installed:

```bash
./venv/bin/python -m pip install imageio-ffmpeg==0.6.0
./venv/bin/python scripts/setup_h3_seed_hunter.py
```

The installer pins dependency revisions, verifies model checksums, keeps an
installation receipt, and refuses to replace modified runtime directories.
It downloads approximately 1.1 GB to readable folders under `models/` on the
Linux filesystem: the learned latent upscaler, DMAD turbo LoRA and RIFE. It
does not download a second H3 model or change the normal H3 default adapter.

Dependency sources and licenses remain with their isolated checkouts:

- [Learned latent upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler),
  revision `40316cf008b2fd8663263270669eb4da23f89d2c`.
- [MiniMaxH3 RefMods](https://github.com/Luisacaotica/ComfyUI-MiniMaxH3Mod),
  revision `f9462081e28794389b5a6c5067eb327412ad8ee7`.
- [KJNodes](https://github.com/kijai/ComfyUI-KJNodes),
  revision `d7641533d6634eb05f8570c980fce8fc8528dc7b`; only its MiniMax module loads.
- [DMAD adapter](https://huggingface.co/Kijai/MiniMax-H3-experimental),
  revision `d8023be02fefbb3633b0cd335c3879f91177299d`.
- [Official RIFE weights](https://huggingface.co/hzwer/RIFE),
  revision `01fdc7e97404120c243c3ea7b427046e5dc7643e`; converted using tensor-only
  loading to safetensors, then run through the pinned inference core's IFNet.

## API and validation

The H3 inventory exposes `seed_hunter` capabilities and a `latent_upscaler`
component role. Normal H3 requests keep their existing validation bounds.
Seed Hunter submissions use `recipe: "seed_hunter"` and bounded nested options.

`GET /api/video/h3/jobs/{id}/seed-hunter` returns safe component IDs and candidate
URLs. `POST .../seed-hunter/finalize` requires `candidate`, a UUID `request_id`
and refinement `config`. It only accepts a completed preview owned by the
requesting user. Duplicate submissions return the original child job, including
after source deletion. No request can supply a latent path or arbitrary module.

Verification includes ownership/isolation, retry concurrency, snapshot lifetime,
nested queue edits, original H3 regression tests, rendered desktop interaction
tests and native iOS tests. A real local GPU smoke test generated 73 frames at
256 × 256 and refined its saved latent to 320 × 320, then interpolated to
146 frames at 48 fps; both outputs retained 3.042 seconds and audio. This proves
the staged path runs, not the quality or VRAM requirements of a full 2 MP render.

Additional real GPU checks exercised continuation (73 source + 39 generated −
22 overlap = 90 output frames, 3.75 seconds with audio), dialogue with FLAC
delivery, and combined first/last guides, preserved audio, RefMod conditioning
and memory optimizations. These used synthetic QA inputs, not user footage.
