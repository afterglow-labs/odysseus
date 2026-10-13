# H3 Seed Hunter

Odysseus's native port of **MiniMax H3 Seed Hunter Latent v2.5.1** by
foxydits / FoxFurEssence Films. Open **Cookbook → Launch → H3 Seed Hunter**
on desktop, or the separate **H3 Seed Hunter** workflow in the iOS client.
It uses the existing private Comfy inference core without a ComfyUI window or
server. It is an implemented recipe, not an arbitrary Comfy graph executor.

## Preview, choose, refine

1. Choose a Ref2VA checkpoint, encoder, VAEs and adapters. The source workflow's
   instance selects Singularity Ref2VA; its internal FL2VA loader default is
   overridden. Other compatible Ref2VA weights remain selectable.
2. Generate one to three previews. They share the prompt and settings and use
   the base seed, base + 1000, and base + 2000. The model loads once per job.
3. Play the results and select a candidate. Selection only opens the refinement
   editor; it does not enqueue anything.
4. Choose the final dimensions, steps, sampler, scheduler, denoise and LoRAs,
   then submit refinement explicitly.

Defaults follow the saved graph: 0.4 MP at 9:16 (480 × 864), 73 frames at 24 fps,
6 steps, ER SDE with beta scheduling. Refinement defaults to about 2 MP at the
preview's actual aspect ratio (1088 × 1952), 2 steps, ER SDE,
linear-quadratic scheduling and 0.4 denoise. Video/audio sigma shifts are
bypassed until explicitly enabled.

The preview's denoised video/audio latent and original conditioning are stored
as a safetensors file plus typed JSON. Refinement uses the learned 3D latent
upscaler on video and retains the audio latent. It does not start from a new
empty video. Each child job owns its snapshot, so deleting a parent cannot
break a queued child. Final jobs retain the original prompt, encoder, model,
codecs, frame count and inputs; rendering parameters and LoRAs remain editable.

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
