# Workflow studio

H3 Video, H3 Seed Hunter and BFS use two views of the same live draft:

- **Pipeline**: choose Inputs, Prepare, Generate, Review, Refine (Seed Hunter), or Export. Model setup is available directly. Every operation needed to run a job is available here.
- **Node map**: select a compact operation to edit its settings in the same inspector. Expand the model setup hub to inspect the base model, LoRA stack, encoder and VAEs. Zoom, fit and scroll the map without changing the job.

The model path shows **base model → applied LoRAs → Generate**. Each adapter modifies that base model. Its name, strength and active state remain visible even when its editor is closed. The expanded setup hub uses short right-angle connections and one bundled connection into Generate.

The current graph describes the supported native recipes. It is not a general-purpose arbitrary wire editor. Component selections, adapters, recipe controls, preview/refinement settings and batch inputs determine the executable recipe; canvas layout is presentation state. This separation also provides the boundary for adding further native graph adapters.

Switching views retains the original inputs, prompt, adapters and forms. Queuing still takes a snapshot of the draft. Review retains pagination, queued-job editing, retries and queue controls. Closing requires the Close button. Server files from E: and F: can be previewed on demand. A source preview does not start rendering.

## ComfyUI conversion

Use **Import ComfyUI** to inspect a UI workflow or API prompt JSON before applying it. The report lists the selected output, components to choose locally, media to attach, and any unsupported nodes or connections. If several video outputs exist, select the intended branch.

**Apply to draft** replaces draft settings and clears regular attachments; it never queues a job. Staged batch files remain in the tray, so review them before submitting with the new settings. Already submitted jobs retain their snapshots. Hardware choices remain local to the current machine. Missing or ambiguous models must be selected explicitly. Missing LoRAs remain visible as unavailable rows rather than being discarded. Required media is listed by filename; imported filesystem paths are not opened.

The ordinary **Import workflow** action also recognizes ComfyUI JSON. Native iOS uses this import path and shows conversion warnings and node diagnostics.

Implemented converters:

- H3 image/first-and-last-frame and reference pipelines using the supported loaders, model LoRA stack, sigma shifting, standard or custom sampler, AV decoding and video output nodes.
- Paired video/audio references loaded through LoadVideo and GetVideoComponents, with native preprocessing disclosed in the report.
- The verified Seed Hunter v2.5.1 graph: its three enabled preview branches become the existing native Seed Hunter recipe. Settings remain editable; unexpected topology or calculator changes produce a report.

These are semantic adapters, not Python node execution. Arbitrary custom nodes and other BFS/LTX/Wan graph shapes currently need additional verified adapters. Unsupported active processing blocks conversion and reports the node type and ID. Disconnected unused nodes do not prevent converting a selected supported output. Compatibility reports can be downloaded without including prompts or the original graph.

Portable Odysseus JSON/ZIP export still uses the existing export dialog, including its choices about model weights and media. Importing a ComfyUI graph does not download model weights automatically.

## Implementation boundaries

`workflowStudio.js` owns presentation and reuses controls from the three workflow panels. `workflowConversion.js` owns the conversion preview; `/api/video/workflows/convert-comfyui` analyzes the graph and resolves local components. `src/comfy_workflow_conversion.py` and `src/comfy_seed_hunter.py` map supported semantics into the existing versioned Odysseus workflow document.

No inference kernels, CUDA configuration, GPU residency behavior or job execution engine were changed by the studio presentation work.
