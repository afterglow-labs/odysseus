# H3 Director

H3 Director is an independent workflow in Cookbook, Pipeline and native iOS.
It implements Muse Director Seed Hunt v1.2.0 with its own timeline and seed
controls. Seed Hunter keeps its separate short-preview/long-final workflow.

## Generate and compare

1. Choose Reference (Omni) or First/Last Frame mode. Both can generate from text
   alone. First/Last Frame accepts either or both endpoint images.
2. Write an overall style prompt and timeline cuts. Cut weights divide the total
   duration proportionally. Describe speech in the cuts and select its speaking
   reference slots; language, soundscape and music have separate controls.
3. Set total duration (1–120 seconds) and chunk duration (3–15 seconds). Each
   chunk is aligned to H3's frame grid, so the encoded duration can be slightly
   longer than the requested timeline. Very short tails are merged or rebalanced
   to keep each model call within 15 seconds.
4. Candidate 1 always runs. Enable candidates 2, 3 or 4 independently. Every
   selected candidate runs the complete timeline at the chosen settings; base
   seeds differ by 1,000,003. Reduce dimensions and duration for cheap scouting.
5. Play/download each result. A completed single-chunk candidate can be explicitly
   refined at 768×1376, 20 Euler/beta steps, denoise 0.2, matching the active
   refiner in the website JSON. iOS also lets you change the final dimensions.
   Refinement resizes and re-encodes the chosen pixels, reuses the exact compiled
   prompt and reference images, and passes its original decoded audio through.

Default generation uses the website JSON's Reference mode, 608×352, 6 seconds,
16 Euler/beta steps, seed 50 and video/audio shifts 12/3. Its 15-second chunk
limit means this default is a single chunk. The Turbo checkpoint-500 EMA LoRA
is applied at strength 1.0 to both generation and refinement. The word `4step`
in its filename does not override the saved graph's 16/20 sampling settings.
The sampling choices
are RES multistep, Euler, Euler ancestral and DPM++ 2M, with simple, normal,
beta or SGM uniform schedules.

The **Apply Director website workflow** button at the top of the desktop/iOS
workflow restores the published model, VAEs, LoRA and generation settings while
keeping the draft's prompt, cuts and reference descriptions/files.
**User-requested encoder override:** Corey explicitly requested Heretic Qwen
wherever a workflow uses Qwen. Director therefore selects
`qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors` instead of the JSON's ordinary
Qwen NVFP4-AWQ encoder. This is a disclosed preference, not exact encoder parity.
Saved drafts and jobs are not silently rewritten. Missing exact components are
reported; the website setup does not fall back to another model or an empty
adapter stack. Custom model/LoRA choices remain editable.

Reference direction supports up to nine images, three videos and three audio
files. Describe characters or mark one image as the location/background. Visual
retention supports preservation, attribute transfer and weak reference. Videos
also have reference, editing-source or continuation-source roles and optional
audio. Audio retention supports timbre/style reference, full/partial copying
and weak reference. Trims refer to the saved file order. Voice-reference slots
pair with corresponding character slots. Later chunks reserve continuity slots,
so the upstream node may use fewer external video/audio references for them.

Hybrid continuation starts with the Reference model and uses a selected
First/Last Frame checkpoint for subsequent chunks. It requires that checkpoint
to be installed; no implicit cross-model fallback is used.

## Saved jobs and boundaries

Director has its own history and queue-edit scope. Original-settings reruns
reuse saved media. Edit & rerun exposes the timeline and candidate controls.
Base-settings reprocessing takes the new timeline/settings while retaining
descriptions and trim indices belonging to the source job's files.

Refinement owns a copy of its source snapshot. Deleting its parent cannot remove
the copy. Retry IDs prevent duplicate submissions. Refinement keeps the original
prompt, models, timeline and media; its size, sampling settings and denoise can
be edited. Nothing refines automatically.

Muse Refine accepts single-chunk candidates; a stitched multi-chunk clip can be
played and downloaded but cannot be passed to that refiner. The integration uses
native form controls rather than the Comfy visual timeline. Reference descriptions
are entered manually; upstream Analyze provider routes are not installed.

## Sources and verification

### Website JSON audit and correction

The audited file is `workflows/muse_minimax_h3_director_scout_v1.json` at Director
commit `d07e8408f3809af7d1406a653da3fb44152b0af6`, SHA-256
`14804582ba07888e8eda1616a06e5ff229caf7bb29df70bf013a2e35c1fa7d73`.
The comparison followed links, including the Set/Get virtual links, the model
switch and node bypass states. It did not infer settings from filenames or the
README's description of the example.

| Setting | Website's active JSON | Original build 61 integration |
| --- | --- | --- |
| Base checkpoints | Stock pruned INT8 Ref2VA / FL2VA | Same checkpoint family; selected by broad name match |
| LoRA | `minimax_h3_turbo_4step_ema_ckpt500_pruned_comfyui.safetensors`, strength 1 | Omitted |
| Text encoder | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | Inherited installed default; Heretic NVFP4 on Corey's server |
| Video/audio VAEs | H3 video FP16 / audio FP32 | Same on Corey's server, but inherited rather than selected explicitly |
| Preview sampling | 16 Euler/beta steps, shifts 12/3 | 20 RES-multistep/simple, same shifts |
| Preview canvas | 0.2 binary MP, 16:9 rounded to 608×352 | 1280×768 |
| Total / chunk duration | 6 / 15 seconds | 10 / 10 seconds |
| Generation seed | 50, fixed | 42 |
| Active refinement | 20 Euler/beta, denoise 0.2, 1 binary MP portrait → 768×1376 | 20 RES-multistep/simple, denoise 0.2, ~1.96 MP at source aspect |

The original stock model/VAE files' whole-file checksums differed from Comfy-Org.
A complete prefix-hash comparison established that each contained the exact
publisher file followed by extra trailing bytes: Ref2VA 73, FL2VA 72, video VAE
61 and audio VAE 61. **No model-weight difference was found.** Clean publisher
copies are now installed, and the website preset prefers their repository paths.

The defaults and explicit website button restore the rows above except the
encoder, which follows the subsequent explicit Heretic preference. Additional
differences in the native integration are still explicit:

- Refinement remains a separate user action, as requested for Odysseus. The JSON
  wires candidate 1 directly through the active refiner to its final MP4 output.
  Odysseus retains the selected candidate's seed; the example's refiner has a
  separate seed with `randomize` selected. Existing candidates retain the actual
  models/LoRAs they were generated with, including old jobs that had no LoRA.
- The KJ live denoising preview / tiny VAE and `iToolsPreviewText` panel are not
  reproduced. Odysseus shows progress and completed candidates and stores the
  compiled prompt in each candidate's metadata. Timeline fields use native forms;
  the upstream reference Analyze/provider UI is not installed.
- The saved JSON bypasses SageAttention, SolAttn, Spectrum, the extra sigma-shift
  patch, RTX upscaling, PurgeVRAM branches and the second refiner. These are not
  missing *active* generation stages. Odysseus does not expose their bypass
  toggles. The README's statement that SolAttn is active differs from the saved
  JSON's `mode: 4`; this integration follows the JSON.
- Odysseus encodes H.264/yuv420p at 24 fps with CRF 18 rather than the JSON's
  CRF 19, uses its own filenames, and stores workflow metadata beside the video
  rather than embedding the entire Comfy graph. It saves tensors before MP4
  encoding for explicit refinement instead of requiring a live graph run.
- Safety/resource adaptations remain: owned local media paths, sequential
  candidate execution, explicit conditioning release, bounded-memory runtime
  operations, 53-bit API seeds, and chunk-tail rebalancing to stay within 15 s.
  The example's sample timeline text is replaced by the user's own directions.

Additional weights obtained during this correction (the ordinary Qwen encoder
is retained on disk but is not selected after Corey's Heretic instruction):

| Repository / pinned revision | File | SHA-256 |
| --- | --- | --- |
| `Momoking/MiniMax-H3-Turbo-Lora-ComfyUI` / `e36a931e03780c01f75f7c4048d6bdf5db4cd509` | `minimax_h3_turbo_4step_ema_ckpt500_pruned_comfyui.safetensors` | `1ad5c813e23c8575bbfa16e7404c0c7f5ef274fb68665f61c860366b3ddd7211` |
| `Comfy-Org/MiniMax-H3` / `912b6e38b2092ec4d2e86d24183e0d59ea059d48` | `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6` |

They are installed under the corresponding repository in Odysseus's Linux
`models` directory. Other workflows' model selections are unchanged.

The corrected setup was exercised on the installed GPUs with Heretic Qwen and
the checkpoint-500 LoRA at strength 1: 608×352 generation completed all 16 steps,
then 768×1376 refinement completed all 20 steps. Both outputs decoded to 158
video frames with audio. The preset application and preserved timeline were
also checked in desktop/mobile Chromium and iPhone/iPad simulator fixtures.

- [Director source](https://github.com/muse-collective-26/MiniMaxH3-Director-Seed-Hunt/tree/d07e8408f3809af7d1406a653da3fb44152b0af6), v1.2.0.
- [Refine source](https://github.com/muse-collective-26/Muse-MiniMax-H3-Refine/tree/e46f0aed164f037bd76dc3b28a77df04df4440b3).
- Vendored inference code retains the Muse MIT license and notice in `licenses/`.
  The port removes upstream HTTP handlers, confines reference paths to owned job
  files, adapts stock node registration, releases conditioning before sampling,
  bounds short-tail chunk merging, and saves candidates one at a time.
- Isolated synthetic GPU runs completed Reference candidates 1 and 3, selected
  candidate pixel refinement, and a two-chunk Reference→First/Last Frame run.
  Browser tests exercise creation and timeline reruns at desktop/mobile sizes in
  classic and Pipeline. Native tests exercise independent creation, queue patches,
  validation and history on simulator fixtures. Smoke tests establish execution,
  not a visual-quality ranking or a guarantee for every 120-second workload.

Powered by MiniMax H3.
