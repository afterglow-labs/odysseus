# Workflow verification — 8 November 2026

The audit compared the installed recipes, native/web defaults, model inventory,
recent job receipts and publisher sources. Existing jobs and saved drafts were
not rewritten. The shared Comfy/PyTorch runtime was not upgraded.

## Confirmed fixes

1. **Seed Hunter and Scout upscaler selection.** Installing H3MD added a
   sharpness upscaler that sorted before Seed Hunter's original model. Fresh
   Hunter presets and refinement forms could consequently select the H3MD
   weights. Hunter/Scout now prefer
   `minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors`; H3MD keeps its own
   `h3_upscaler_sharpness_2000steps_v0.1_fp32.safetensors`. The native client
   follows the server's advertised choice. Explicit saved choices remain intact.
   A regression test failed with the original selection and passes with the fix.
2. **Continuation encoding.** A recorded continuation failed with
   `Unknown encoder libx264`. The FFmpeg first on the app's PATH lacks that
   encoder. Continuations now use the existing capability-checked FFmpeg
   selector used by playback previews. A real H.264/AAC MP4 was encoded and
   decoded successfully under that PATH.

## Results by workflow

| Workflow | Source and configuration assessment | Installation and runtime evidence |
| --- | --- | --- |
| H3 Seed Hunter | Native adaptation of publisher **2.5.1**. Publisher **3.0** appeared on November 5 with a different upscale path and other controls. Native quality deliberately uses 20 steps without a speed LoRA or sparse attention; the original connected 2.5.1 example uses six ERSDE/beta steps with DMAD and SLA. These are distinct recipes. | Native quality/turbo dependencies resolve. Importing the original graph requires its exact v1.3 checkpoint/int8 VAE and resolving duplicate audio-VAE candidates. Upscaler and continuation defects fixed. |
| H3 Seed Scout | Installed author revision matches the current repository. Scouting executes the first N steps of the full schedule and continues the selected saved latent with the remaining schedule. Selection stays explicit. | Source/backend/browser selection tests pass. Optional finishing now receives the correct Hunter upscaler. |
| Muse H3 Director | Installed revision matches current publisher revision d07e8408. Publisher graph SHA-256 matches the recorded hash. Its 16-step Euler/beta candidate path, independent seeds and refinement settings are separate from Hunter. Heretic is an existing explicit encoder adaptation. | Required files resolve. Recent real jobs include a completed 16-step Director run. Candidate/refinement tests pass. |
| H3MD Director | Pinned to 5c217062; current publisher is v0.3.6 at e48bed0c. Newer optional controls are not part of this pinned integration. Core uses the hybrid model, DMAD + LightX2V v1.3, six low-resolution steps and two final-resolution steps with its sharpness upscaler. | Core is ready. The optional complete eight-adapter preset lacks MiniMaxH3_Enhancer_0.5-1.0_3432130_epoch_10.safetensors and is disabled. Two older jobs ended without completion receipts and have empty worker logs; their cause is not established. |
| PinkCherry | Checked directly against SexGod1979's current hybrid v1.8 workflow on Hugging Face, revision 08ac92dbd56a8523beea55aafaaba1a1d72c45cb. Linked controls select 12 steps, not the scheduler widget's unused 20; Euler/simple, exact non-768p FL Turbo v1.0 LoRA, no added sigma shift, 0.8 MP portrait and 294 frames. Alternate switch selects four steps. | Model, LoRA, Heretic and VAEs resolve. The publisher currently lists the same pruned v1.8 checkpoint used here. Heretic and bounded attention differ from its graph. A live 12-step job completed during the audit; this does not prove identical output to ComfyUI. |
| H3 VFX / base H3 | Installed VFX preset resolves Ref2VA, Heretic, stock VAEs and the VFX adapter. Source frames use the aligned guide at frame zero; the vfx_edit: trigger is normalized. Native settings use a single-pass 20-step recipe, rather than every optional stage of the current author example. | Required files resolve. Tests cover source/reference separation, prompt trigger, migration, encoding and persistence. |
| BFS H3 head swap | Matching head-swap adapter and aligned source-guide conditioning. Native 20-step single-pass default is a documented adaptation; the latest author example also contains speed/upscale stages. | Required files/runtime present. Conditioning/media tests pass. |
| BFS LTX-2 v1 | Prepared first-frame identity; optional prepared last frame requires its matching adapter. | Missing LTX-2 transformer, Gemma encoder, both VAEs and text projection. Not ready. |
| BFS LTX-2 v2 | Aligned white head-mask video; worker creates the magenta guide. | Same missing base components as LTX-2 v1. |
| BFS LTX-2.3 v3 | Persistent identity-panel conditioning; distilled base and matching adapter. | Missing distilled transformer, Gemma encoder, both VAEs and text projection. |
| BFS LTX-2.5 v1 | Author helper supplies overlapping source/identity conditioning. | Missing distilled transformer and Gemma encoder. VAEs/adapter present. |
| BFS LTX-2.5 v1.1 | Distinct v1.1 adapter with default strength 0.8. | Missing distilled transformer and Gemma encoder. |
| BFS Wan 2.2 | High/low-noise Bernini pair and corresponding BFS adapters. | Missing both Bernini base transformers. Encoder/VAE/adapters present. |

The BFS repository supplies adapters; downloading it does not install every
base-model family. Native BFS executes its documented generation/conditioning
stage. Optional first-frame editors, captioners and upscale passes from publisher
graphs are not run implicitly.

## Actual job limits

A failed Hunter refinement used 1952 × 1088, 362 frames and six final steps.
Its first failure was a QKV projection requesting another **19.85 GiB** with
**12.14 GiB** free on the 5090. This establishes a VRAM capacity failure for
that configuration, not an upscaler failure. A separate Scout refinement was
rejected because its model differed from the saved candidate; that consistency
check should remain.

Source/regression checks do not prove output quality or full GPU execution for
every combination of model, resolution, duration and reference count.
Uninstalled BFS families were checked structurally, not rendered. The two
empty-log H3MD failures remain unclassified.

## Primary sources and evidence

- [Seed Hunter publisher/version history](https://civitai.com/models/2881362)
- [Seed Scout](https://github.com/tsuremen2/ComfyUI-H3-SeedScout)
- [Muse Director original graph](https://github.com/muse-collective-26/MiniMaxH3-Director-Seed-Hunt/blob/d07e8408f3809af7d1406a653da3fb44152b0af6/workflows/muse_minimax_h3_director_scout_v1.json)
- [H3MD pinned source](https://github.com/1113800942/comfy-h3md-director/tree/5c2170620abd3baf739b4be7cb72960be99d713a)
- [H3MD upstream changes](https://github.com/1113800942/comfy-h3md-director/compare/5c2170620abd3baf739b4be7cb72960be99d713a...e48bed0c1fbd9589fa62d6852ae83e75734f3ded)
- [PinkCherry original graph](https://huggingface.co/SexGod1979/PinkCherry-NSFW_MiniMax-H3/blob/08ac92dbd56a8523beea55aafaaba1a1d72c45cb/1.8-hybrid/PinkCherry_ref2va_turbo_lightx2v_workflow.json)
- [VFX publisher guide](https://huggingface.co/Alissonerdx/Minimax-H3-ComfyUI/blob/64244a4fdf226c8d37b8470d1893b6ba9ab54de1/docs/vfx-edit.md)
- [BFS models/workflows](https://huggingface.co/Alissonerdx/BFS-Best-Face-Swap-Video/tree/d0adb5394c6a1b05f2245ec33cb91051a8000853)

Source graphs, hashes, inventory snapshots and the continuation encoding sample
are under `outputs/workflow-audit/`. These are diagnostic artifacts, not
replacements for saved user workflows.
