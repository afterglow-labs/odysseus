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
   refined at approximately 1.96 MP, 20 RES-multistep/simple steps, denoise 0.2.
   Refinement resizes and re-encodes the chosen pixels, reuses the exact compiled
   prompt and reference images, and passes its original decoded audio through.

Default generation uses Reference mode, 1280×768, 10 seconds, 20
RES-multistep/simple steps and video/audio shifts 12/3. The sampling choices
are RES multistep, Euler, Euler ancestral and DPM++ 2M, with simple, normal,
beta or SGM uniform schedules.

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
