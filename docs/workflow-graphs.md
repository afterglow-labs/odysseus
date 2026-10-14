# Editable workflows in Odysseus

Open **Cookbook → Workflows** to build a workflow. **New workflow** offers a blank graph and editable templates. The existing H3, BFS, and Seed Hunter panels remain available as quick workflow forms; **Edit workflow graph** opens their current settings in the builder.

## Pipeline and Nodes are two editors for the same workflow

- **Pipeline:** select an operation, edit its settings, and choose its upstream operations under **Connections**. You can add operations and change the execution order without opening the canvas.
- **Nodes:** click an output port and then an input port, or drag between them. Select a connection to change its source or delete it. Changing a wire changes what executes.
- Both views use the same parameters and edges. Switching views keeps your changes. **Undo** and **Redo** work across both views.
- Drag blocks to move them on the snapped grid. **Arrange** and **Fit** change the layout without changing connections. Setup hubs can collapse the model, LoRAs, encoder, and VAEs into one bundle on the canvas.
- An operation's detailed controls live in the inspector. **Enabled**, **Disabled**, and supported **Bypassed** states affect execution. Bypass requires a declared pass-through connection.

The builder closes only through **Close**. Clicking outside or pressing Escape does not discard the workflow. Draft settings and server-file selections are kept in this browser. Browser security prevents restoring local uploaded `File` objects after closing/reloading the page; select those files again when needed.

## Models, LoRAs, and inputs

Native H3 workflows use a **Model setup hub**. Connect the model through one or more **Apply LoRA** operations before the hub:

```text
Model → Apply LoRA → Apply LoRA → Setup hub → Generate
Encoder ────────────────────────────↑
Video VAE ──────────────────────────↑
Audio VAE ──────────────────────────↑
```

Choose each installed component and LoRA strength in its inspector. One setup connection carries all of those identities to generation; collapsing the setup group does not remove its operations.

Input operations hold uploaded media or a selected server file. Video inputs support **Server files · E: / F:** so the video stays on the Odysseus computer. Reference bundles distinguish first/last frames, the aligned source video, and numbered references. Multiple references are connections into the same reference bundle; they are separate from submitting separate jobs.

Package-defined media controls also support uploads. When a package declares several media controls, each has its own attachment. The graph JSON stores media names/settings, not the media bytes.

## Check, save, and run

1. Use **Check workflow** to see the compiled plan and any missing components, required connections, unknown operations, or invalid parameters.
2. Use **Save** to store the graph in this server's **Library**. **Export JSON** downloads the graph, settings, and layout as an `.odysseus.json` document. It does not bundle model weights or attachments.
3. Choose source files and press **Queue workflow**. Each submission captures its own graph and attachments. Further edits affect the next submission, not the queued one.
4. Open **Recent workflow runs** for status, step details, cancellation, and output downloads. The queue continues if the builder is closed.

**Edit as new draft** restores a run's graph settings for another submission. Review/reselect attachments before queuing it. It does not immediately start a render.

Native video outputs offer playback and a video download. Generic Comfy package runs provide **Download results ZIP**: saved files are under `outputs/`, and `result.json` contains the execution result and any text/UI result data. The ZIP is not a video file.

The native **Seed Scout** template has an explicit preview choice. When it reaches **Choose a Seed Scout preview**, select **Continue with this seed**. The chosen seed resumes that job. Refreshing status keeps unchanged previews mounted.

## Importing ComfyUI JSON

**Import JSON** accepts supported ComfyUI editor workflows/API prompts and Odysseus graph documents. The import retains original node data and connections so missing support can be resolved rather than silently discarding parts of the workflow.

- Known native operations can compile into Odysseus H3, BFS, Seed Hunter, Seed Scout, or media-processing steps.
- Discovered Comfy node classes contribute their actual input types, combo choices, defaults, numeric limits, output types, names, and execution declarations.
- Unknown or unavailable operations remain visible. An enabled output that depends on them cannot run until its implementation is available or its connection is deliberately changed.
- Imported model filenames still need to resolve to installed components. Importing JSON does not download models or install Python dependencies.
- A required connection must be wired. A **connection-only** field is a package's `forceInput` declaration. Where a widget also accepts a connection, the wire overrides the saved widget value; disconnect it to use that value again.
- A **list** input receives the package's list value through its connection. It does not mean arbitrary extra wires may be added to a single-input port. The execution core preserves declared list mapping/broadcasting behavior.

Select **Package input rules and schema** in an operation's inspector to inspect its original schema, hidden inputs, and validation/change-detection hooks. Hidden inputs such as prompt data and unique IDs are supplied by the execution core.

## Adding an installed Comfy node package

This is an administrator operation on the Linux Odysseus server. Package discovery uses Odysseus's private Python and private inference core. It does not start a ComfyUI web server or install dependencies automatically.

1. Put a checkout of the package in:

   ```text
   /home/corey/projects/odysseus/runtimes/comfy-packages/sources/<package-name>/
   ```

   The package must contain `__init__.py` or `nodes.py`. A standalone `.py` package module can also be placed in the `sources` directory. The candidate list also includes existing Odysseus/private-runtime `custom_nodes` folders, built-in `nodes.py`, and private-runtime `comfy_extras/nodes_*.py` modules.

2. If the package requires dependencies, inspect its own installation instructions and install the necessary dependencies into Odysseus's environment. For a reviewed `requirements.txt`, the command is:

   ```bash
   cd /home/corey/projects/odysseus
   ./venv/bin/python -m pip install -r runtimes/comfy-packages/sources/<package-name>/requirements.txt
   ```

   Replace `<package-name>` with the actual folder. Use this private interpreter, not global `pip`. A package may require versions incompatible with the current inference environment; its requirements still need review.

3. Open **Workflows → Node packages**. Choose the installed package, then **Discover package**. Discovery imports its Python declarations in a separate CPU process and records the source fingerprint. Package import code is trusted executable code; process isolation is not a security sandbox.
4. Review the package report. A package can be partly discovered even if an individual class fails. Missing dependencies are listed without automatically installing them. **Inspect declared inputs, outputs and hooks** and **Export schema** show what was discovered.
5. Return to the workflow. The refreshed **Add operation** list includes the discovered classes. Import the workflow JSON again if it previously lacked those classes, then check the resulting graph and its required model/media selections.

Private manifests are stored under:

```text
/home/corey/projects/odysseus/runtimes/comfy-packages/manifests/
```

If package source changes, discover it again. The worker verifies the source fingerprint before execution. Rediscovery also refreshes model/file combo choices that a package produces from its installed model folders.

### Metadata from another ComfyUI installation

Use **Node packages → Import /object_info JSON** to load exported node metadata. This supplies schemas for inspection and conversion. It does not supply Python code: execution remains unavailable until matching installed classes are discovered. Installed class declarations take precedence over an unbound metadata export; conflicting installed classes must be resolved.

## Current compatibility boundaries

Discovery supports legacy `NODE_CLASS_MAPPINGS` and newer extensions that expose an official schema projection. Registered package graphs execute through the private Comfy execution core, including declared hidden inputs, list semantics, validation hooks, and cache/change hooks.

This does not automatically reproduce a package's custom ComfyUI browser JavaScript. A package may need an additional Odysseus control or event bridge for a specialized interactive editor, custom picker, frontend-only prompt expansion, or callback. Its plain declared widgets and connections can still be inspected. Metadata discovery alone is not proof that every node in that package has been render-tested.

Comfy outputs are collected from the individual Odysseus job directory. The worker rejects absolute paths and parent-directory traversal in common output-path settings and collects only files owned by that job. Trusted package code is not sandboxed, so this is not a guarantee about every package-specific file-writing control. Native workflows and arbitrary package graphs have different component contracts; connect ports according to their declared types rather than assuming every `MODEL` or encoder wrapper is interchangeable.
