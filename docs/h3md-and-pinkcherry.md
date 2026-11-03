# H3MD Director and PinkCherry

These are separate Cookbook and Pipeline workflows. Muse H3 Director and H3
Seed Hunter keep their own drafts, history, presets and execution paths.

## H3MD Director

Based on **H3MD Director (ComfyUI-H3MD-Director) by YJ**, pinned to
[`5c2170620abd3baf739b4be7cb72960be99d713a`](https://github.com/1113800942/comfy-h3md-director/tree/5c2170620abd3baf739b4be7cb72960be99d713a).
The installed source retains its AGPL license. Odysseus loads its inference
modules inside the existing embedded Comfy runtime; its web editor and HTTP
routes are not started.

Choose **H3MD two-pass**, write the overall prompt and add shots. The default
uses the author's hybrid FL/Reference checkpoint, DMAD and LightX2V v1.3 LoRAs
at strength 1, the sharpness latent upscaler, stock video/audio VAEs and the
Heretic Qwen encoder. The source's ordinary Qwen is replaced with Heretic at
the user's request.

The default is eight Euler/simple steps: six at half size, learned latent
upscaling, then two at the final 704 × 1280 size. Both passes use the same
checkpoint, with independently editable LoRA strengths. Shots may start with
a cut or continue with 22/39-frame overlap. Longer shots are automatically
split by the pinned upstream planner. Lengths round to supported frame counts
at 24 fps; the form accepts up to ten minutes across 64 shots.

Reference selections belong to each shot. An empty selection uses all attached
references of that type. Audio can act as a reference or a locked soundtrack.
Outputs contain generated video and audio. Segment frames and latents remain
on disk for explicit **Edit & rerun → Resume saved segments**. Enter segment
numbers such as `2, 4-6` to redo those segments. Reruns preserve the original
job and detach shared cache files before writing replacements. Changing the
checkpoint, encoder, VAE, upscaler or LoRA weights requires disabling resume.
Unchanged cached segments are reused; changed prompts/settings are checked by
the upstream cache logic.

**Author’s complete LoRA setup** preserves all eight adapters and their
separate pass strengths from the lite example, including its 1472 × 1088
dimensions and color anchor. It is disabled while any required file is
missing. In this installation the missing file is
`MiniMaxH3_Enhancer_0.5-1.0_3432130_epoch_10.safetensors`, linked from
[the author's Enhancer page](https://civitai.com/models/3025511/minimaxh3enhancers).
The server received HTTP 403 when attempting its download. It is not silently
omitted from that preset. The core two-LoRA preset runs independently.

This integration covers the multi-shot, two-pass, learned latent upscale,
continuation and resumable segment path. The optional source graph's
SAM3/depth/motion controls, RefMods/semantic bridges, SigmaRefiner, face
refinement and RIFE output interpolation are not exposed. It uses Odysseus's
bounded attention implementation and upstream chunked feedforward execution;
SageAttention is not installed. These are explicit adaptations, not a claim
that every node in the upstream sample graph is reproduced.

Install the pinned source and verified model additions with
`venv/bin/python scripts/setup_h3md.py`. Download revisions, sizes and SHA-256
hashes are recorded in `scripts/h3md_models.json`. The existing H3 setup
supplies DMAD, Heretic and the stock VAEs.

## PinkCherry

The source is the user-supplied
`PinkCherry_ref2va_turbo_lightx2v_workflow.json`. The standalone default follows
its connected 12-step path, and a second preset follows its four-step switch.
Both use Euler/simple, denoise 1, no sigma shift and the same LoRA at strength 1.
There is no seed preview or upscale stage in this graph.

The selected LoRA is exactly
`minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`.
The similarly named `_768p` file is a different published file and is not
substituted. Its verified download is included in the preset model manifest.
The default output is 672 × 1216, 294 frames (12.25 seconds), 24 fps,
H.264/yuv420p at CRF 9 with audio.

Explicit source adaptations:

- The user-supplied pruned PinkCherry hybrid v1.8 checkpoint replaces the
  unpruned filename in the JSON.
- Heretic Qwen replaces the ordinary Qwen encoder, as requested.
- Odysseus's existing bounded attention replaces the SageAttention node.
- The TinyVAE intermediate preview node is not used. The final output uses
  the full video VAE; Odysseus supplies job progress and a final video player.
- Source example prompts and media are not copied into new jobs.

Applying either preset keeps the current prompt and attached references.
At least one reference image or video is required. PinkCherry is also still
available among Seed Hunter's checkpoint presets, but that remains a separate
preview/refinement workflow with different execution settings.
