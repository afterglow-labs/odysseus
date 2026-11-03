# LoRA help

Choose **About this LoRA** beside a selected adapter or in the LoRA library.
The iOS and web clients show its purpose, trigger instructions, usage, settings,
known documentation gaps, and links to its README or author workflow. Copying
a trigger copies text only; it does not change a prompt or submit a job.

The offline catalog in `scripts/h3_lora_help.json` currently covers 30 distinct
adapter filenames. Duplicate installed copies share the same documentation.
H3 inventory exposes `component.lora_help` for every discovered LoRA; known BFS
adapters expose the same field. Known catalog entries also let H3 discover
adapter files whose filenames lack an H3 prefix, after validating their tensor
header. A checkpoint with a matching filename is not treated as an adapter.

Documentation is separate from `lora_profile`. Empty, customized, and frozen
profiles retain their own execution settings and do not suppress publisher
help. Reading help never changes compatibility, strength, workflow selection,
prompt, or sampling settings. Existing jobs retain their original profiles.

Each catalog entry contains `title`, `summary`, `trigger`, optional copyable
`trigger_words`, `usage`, `settings`, `caveats`, `sources` (label/URL), `coverage`,
and `verified_on`. Settings distinguish publisher instructions from an Odysseus
preset or embedded file metadata. `partial` means the sources lack an exact
version's recipe or validation; an unknown adapter receives an explicit
`unidentified` fallback rather than invented instructions. A missing trigger
is not evidence that no trigger is needed.

To add or revise help, inspect the exact file version and its primary model
card/workflow, summarize it in English, link the source, and record the check
date. Do not copy a newer release's triggers or sampling recipe onto an older
filename. Keep full model cards on the publisher site. The inventory reads a
small bundled catalog and never fetches remote documentation in a request.

Focused validation: `tests/test_h3_lora_help.py` checks discovery, catalog
coverage, profiles, fallback behavior and BFS parity.
`tests/test_h3_lora_help_browser.py` opens every catalog entry at phone and
desktop widths and verifies that copying help leaves draft values intact.
