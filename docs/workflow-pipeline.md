# Build Pipeline

Open **Cookbook → Launch → Build Pipeline**, then choose H3 Video, H3 Seed Hunter, H3 Seed Scout, or BFS Video. The existing classic launchers remain available alongside it.

The workspace follows the approved pipeline mockup: stages on the left, source media and results in the center, and the selected stage's actual settings on the right. Narrow screens stack those areas. **Show all settings** exposes the controls together.

This is a separate presentation of the existing native workflow forms. The input pickers, LoRA editor, config serialization, queue submission, job editing and playback remain the same code paths. The pipeline has independent draft and batch-session keys so experiments do not replace a classic draft. Node-map development remains separate.

## Editing and playback

- **Inputs** contains local and E:/F: server-file selection, reference roles, section selection, and the existing batch controls. Multiple references belong to one job unless Batch job is enabled.
- **Model setup** contains the actual model, encoder, VAEs, compatible upscaler and named GPU selections.
- **Prepare** contains the prompt, enhancement model and LoRA stack. A LoRA's enabled checkbox changes its actual strength to zero; enabling it restores its previous signed strength.
- **Generate** contains dimensions, duration, steps, seed, sampler, scheduler and other controls supported by the selected recipe.
- **Review** plays saved results and previews in the browser. Seed Hunter selection opens its refinement editor; Seed Scout selection explicitly continues that same job's partial latent.
- **Refine** appears for Seed Hunter's saved-latent refinement. It requires a separate render action.
- **Export** uses the existing Odysseus workflow export choices. Export to a ComfyUI node is deferred.

Changing stages retains the original form controls and attachments. Playback is not restarted merely by a status poll. Jobs retain their submitted settings, and the server queue continues while the panel is closed. Close requires the Close button.

The source/result selectors show real media; the mockup's illustrative footage and simulated timeline are not presented as working controls. This initial pipeline supports the existing native recipes rather than arbitrary user-wired graphs.
