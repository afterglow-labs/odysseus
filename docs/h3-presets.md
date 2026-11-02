# H3 preset recipes

Source audit: November 1–2, 2026. The picker appears above the creation controls
on desktop and native iOS, including Pipeline. The server advertises each preset
with its resolved component IDs, readiness, missing files and refinement recipe.
Selecting a preset preserves the prompt, media, ranges, durations and GPU choices.
It replaces the model, video VAE, sampling recipe and adapter stack shown in the
form. Saved jobs retain their own settings.

| Preset | Seed previews | Final finishing pass | Components / purpose |
| --- | --- | --- | --- |
| Singularity quality | 20 ER-SDE / beta | 2 ER-SDE / linear-quadratic, denoise 0.4 | Singularity V2 pruned INT8; no acceleration adapter |
| LightX2V fast | 4 Euler / simple | Quality two-step finish | Ref2VA Turbo 4-step v1.0, 768p |
| LightX2V balanced | 8 Euler / simple | Quality two-step finish | Ref2VA Turbo 8-step v1.1, 768p |
| Pika Flex | 6 LCM, manual sigmas | Quality two-step finish | Pika Flex Ref2VA v1; requires image or video references |
| Singularity + LightVAE | Quality 20-step recipe | Quality two-step finish | Smaller LightVAE encoder/decoder, converted INT8 weights |
| Muse Director · Reference mode | 16 Euler / beta; up to four candidates | 20 Euler / beta, denoise 0.2; pixel resize and re-encode | Weighted timeline cuts; decoded preview audio passes through the final output |
| Singularity × 10Eros | 8 Euler / simple | 8 Euler / simple, denoise 0.4 | PRISM beta 7 INT8 HQ; Turbo already merged |
| Character Swap V1.5 | 8 ER-SDE / normal | 8 ER-SDE / normal, denoise 0.4 | Character adapter + publisher's FL2V Turbo 8-step v1.0 pairing |

Preview length and final length are separate. A longer final requires a full
generation at preview resolution using the selected seed before the finishing
pass above. Quality defaults to a short 73-frame preview and approximately
2 MP final dimensions; changing presets preserves your chosen durations and
preview size. Refinement is queued only by an explicit user action.

## Sources and compatibility

- [Singularity](https://huggingface.co/WarmBloodAban/Minimax-h3_Singularity):
  V2 remains the selected quality checkpoint. The existing Seed Hunter
  implementation supplies the learned latent upscaler and two-step finish.
- [LightX2V weights](https://huggingface.co/lightx2v/Minimax-h3-Turbo) and
  [publisher recipes](https://github.com/ModelTC/Minimax-H3-Turbo): the new
  768p Ref2VA files are installed. The 6/3 video/audio shifts and matched
  reference sizing are starting values from the publisher's 768p Turbo recipe;
  its README still documents the older Ref2VA v0.1/544p row separately.
  This audit did not establish a visual optimum for the new Ref2VA files.
  Turbo adapters are removed from the classic two-step finishing pass.
- [Pika Flex](https://huggingface.co/Pika-Labs/Pika-H3-Flex) and
  [Comfy's reference template](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_pika_h3_flex_ref2va.json):
  LCM uses `1, .972973, .923077, .8, .571429, .333333, 0`, with no second sigma
  shift. The six-step adapter is removed for the two-step finishing pass.
  This manual schedule is supported in Seed Hunter, not Scout continuation.
- [LightVAE](https://huggingface.co/lightx2v/LightVAE) and
  [Kijai's conversion](https://huggingface.co/Kijai/MiniMax-H3-experimental):
  the bundled runtime needs a targeted loader for the pruned encoder. The
  loader is limited to the exact LightVAE filename and leaves normal VAEs alone.
- [Muse Director Seed Hunt](https://github.com/muse-collective-26/MiniMaxH3-Director-Seed-Hunt)
  at `d07e8408f3809af7d1406a653da3fb44152b0af6`, with
  [Muse Refine](https://github.com/muse-collective-26/Muse-MiniMax-H3-Refine)
  at `e46f0aed164f037bd76dc3b28a77df04df4440b3`:
  native Reference-mode adaptation for single clips up to about 15 seconds.
  Each line is a CUT; `2 | Wide shot` followed by `1 | Close-up` divides the
  chosen duration 2:1. Candidate seeds differ by 1,000,003. Audio and music
  instructions have separate fields. This port does not implement upstream's
  visual timeline, provider analysis, FL2VA/hybrid mode or multi-clip stitching.
  It does not load upstream HTTP routes. Powered by MiniMax H3.
- [PRISM](https://huggingface.co/OliviaRossi/Minimax-h3_Singularity-x-10Eros-Max_PRISM):
  `10Eros_Max_h3_8stepTURBO_beta7_SingularityV2_int8HQ.safetensors` is a selectable
  comparison, not a proven replacement for Singularity. Its eight-step Turbo
  is baked in, so the preset adds no second Turbo adapter.
- [Character Swap](https://huggingface.co/akatz-ai/MiniMax-H3-Character-Swap-LoRA):
  V1.5 at strength 1 plus FL2V Turbo 8-step v1.0 at strength 1, with Ref2VA,
  eight ER-SDE/normal steps and video/audio shifts 12/3, follows the publisher's
  sample. Attach the performance as **Video 1** and identity as **Picture 1**.
  This specific published adapter pairing is allowed; unrelated FL2VA adapters
  still require their matching model. The preset supplies the model setup;
  it does not add the full Relay workflow's masks or scene-anchor processing.

The other video workflows were checked against their publishers. BFS H3 v1.0
and BFS LTX-2.5 v1.1 were already supported; the audit found no newer BFS release
to install. See [BFS](https://huggingface.co/Alissonerdx/BFS-Best-Face-Swap-Video)
and [LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5). Standard H3 and Scout
share the installed model inventory; standard H3 now also accepts ER-SDE.
Scout's partial-step cutoff must remain below its selected full step count.

[Open-H3-Regenerate-2K](https://huggingface.co/Efficient-Large-Model/Open-H3-Regenerate-2K)
was reviewed but is not advertised as a working preset: its complete published
pipeline requires a different checkpoint and additional adapter/engine setup.
Installing the refiner alone would not reproduce that workflow. No broad
PyTorch, CUDA or embedded Comfy update was made.

## Installation and validation

Optional weights are pinned by repository revision, byte count and SHA-256 in
`scripts/h3_seed_hunter_preset_models.json`. Install with the official `hf` CLI:

```sh
venv/bin/python scripts/setup_h3_presets.py --preset character_swap
venv/bin/python scripts/setup_h3_presets.py --preset all --verify-only
```

The installer writes under the project's Linux `models` directory. Refresh H3
inventory afterward. An unavailable preset shows its missing components rather
than silently substituting another model.

Verification includes configuration resolution for all eight presets, distinct
preview/final schedules, longer final clips, four Director candidates, Pika
sigmas, adapter pairing, prompt edits and idempotent retry. Browser tests cover
desktop/mobile classic and Pipeline forms. Native tests use isolated iPhone and
iPad simulator fixtures. An isolated synthetic Character Swap GPU run completed
all eight steps and decoded 73 video frames plus audio. LightVAE completed a
GPU encode/decode smoke test. These establish compatibility; no visual-quality
ranking across presets or physical-iPhone crash reproduction is claimed.
