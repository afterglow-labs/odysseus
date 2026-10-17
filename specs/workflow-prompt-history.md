# Workflow prompt history

The main prompt controls in H3, Seed Hunter, Seed Scout, BFS, and Build Pipeline
share a server-backed history. Desktop and iOS use the same signed-in owner's
entries. Imported native workflows and explicit main-prompt fields in the workflow
editor use the same library. Choosing a saved prompt changes only the prompt; it never submits a job,
changes settings, or removes attached media. Loading or refreshing history does
not replace an unsaved draft.

Accepted workflow jobs save their main prompts automatically. The history control
also offers an explicit save action for a prompt that has not been submitted.
Typing alone does not create partial entries. Existing owned job records seed the
history; BFS uses its editable prompt notes rather than generated instructions.
Negative prompts and dialogue-builder fragments are not added as main prompts.

History lives in `video_jobs/.prompt_history/history.sqlite3` under the app data
directory. Entries are unique per owner
after normalizing line endings and trimming outer whitespace. Internal formatting
and case are retained. Reusing a prompt updates its last-used time and moves it
up the list without creating a duplicate. Entries remain available after video
jobs are deleted; there is no automatic expiry or retention cap.

`GET /api/workflows/prompt-history` accepts `q`, `limit`, and `offset` and returns
`prompts`, `total`, and `next_offset`. Entries contain `id`, `prompt`, `workflow`,
`created_at`, and `last_used_at`. `POST` to the same path accepts `prompt` and an
optional `workflow` and returns `entry` and `created`. History reads and writes
use the workflow authentication and ownership rules.

Validation must cover persistence, concurrent duplicate saves, owner isolation,
old-job import, and reuse across workflow families. Rendered desktop/mobile and
native iOS checks must preserve drafts during refresh and apply the complete
selected prompt without submitting a render or changing other controls.
