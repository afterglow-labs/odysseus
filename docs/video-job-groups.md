# Video job groups

New batch submissions carry one stable batch UUID across every file and upload
retry. The iOS history shows one master card per batch, with paginated file jobs
inside. Hunter and Scout results stay under their source preview job. Older
batches without a saved batch UUID remain individual jobs; timestamps or prompt
similarity are not used to guess batch membership.

Opening a file job exposes Cancel/Stop, Delete, and Process now for a waiting
job. Process now changes queue priority; it does not interrupt the current
render or resume a paused queue. Long-press a card for group actions:

- Stop all processes: stop running work and cancel waiting descendants.
- Refine all unrefined: open the shared candidate/refinement settings panel.
- Duplicate: create fresh source jobs using their saved inputs and settings.
- Rerun all: explicitly rerun sources and existing refinements with their own
  saved seeds and settings, grouped under the new source jobs.
- Delete job and all files: stop and remove the entire owned group, including
  queued children, previews, saved input copies, latents and registered outputs.
  Original source media and model files are not job storage and remain intact.

Deleting a batch also cancels its pending iOS uploads. Server tombstones reject
late submissions to deleted groups, and deletion waits for supervisors to exit
before removing their files. Partial cleanup failures remain retryable.

Preview generation never schedules automatic refinements. Legacy `auto_refine`
draft settings and completion tickets cannot queue new GPU work. Explicit
manual/bulk refinement actions continue to publish their requested outputs.

The grouped history API is opt-in (`group_batches=true`); existing flat-history
clients retain their previous response format. Group actions are owner-scoped.
