# Odysseus workflow studio: agreed design direction

This directory preserves the approved interactive design prototype. The production implementation is documented in [workflow studio](../docs/workflow-studio.md); the HTML here remains a simulation.

Corey chose the hybrid direction: a pipeline workspace and a clean, expandable node map editing the same workflow definition.

The objective is to improve the workflow interaction model, not reproduce ComfyUI with a different theme. Start with task-sized operations such as Generate, Review and Refine. Expose lower-level components only when the user expands an operation. Judge the design by how easily a user can understand, change and run a workflow, rather than by resemblance to an existing node editor.

## Keep connections readable

- Group related components into named setup hubs. Use one inspectable, typed bundle between groups instead of repeating individual model, encoder, VAE and adapter wires across the canvas.
- Hubs must have a meaningful purpose. Do not replace visible spaghetti with an opaque universal bundle; show its contents, source and overrides in the inspector.
- Collapse internal wiring by default. Expand a group locally when the user needs to inspect or change a specific component.
- Use compact nodes, aligned ports, consistent spacing and orthogonal connections routed around blocks. Automatic layout must avoid overlapping nodes, labels and connections.
- Keep prompts, sliders, advanced settings and long filenames in an inspector, rather than enlarging every canvas node.
- Show real branches explicitly, such as alternate refinements feeding a comparison step. Repeated batch jobs belong in the batch table, not hundreds of duplicate nodes.

## Make model connections understandable

- Show the base H3 model, its applied LoRA stack and the resulting connection to Generate in the pipeline view.
- Give every adapter its own name, strength and enabled state. Several enabled adapters modify the same base model; do not depict them as successive video effects or separate base-model instances.
- Keep the active adapter summary visible when details collapse.
- Keep prompt/reference inputs and encoder/VAE responsibilities distinguishable when expanding the setup group.

## One recipe, multiple views

- Switching views preserves selection, draft settings, adapters and per-clip overrides.
- Distinguish batch defaults from clip overrides and provide a reset to inherited settings.
- Submitted jobs retain immutable parameter snapshots. Editing a new batch or moving nodes must not change queued jobs or trigger execution.
- Keep canvas layout, workflow definition and runtime status separate. GPU model residency requires server implementation independent of the editor.
- On narrow screens, retain access to the inspector, batch controls and previews without requiring precise wire dragging.

The HTML uses illustrative preview art and sample data. Queue and playback actions are simulations; exported sample JSON is not a production workflow format.
