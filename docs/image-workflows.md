# Image workflows

Open **Cookbook → Launch** and choose **LUSTIFY Inpainting**, **Qwen Rapid Image
Edit**, or **SNOFS Krea 2 Turbo**. Each has its own settings and result history.
Jobs run on the server and keep running when the client closes. An explicit Stop
request cancels only that job. Results are private to the signed-in owner.

The default GPU is the installed RTX 4090 when present. A server can pin its
choice in `IMAGE_WORKFLOW_DEFAULTS_FILE` with `{"gpu": "GPU-<uuid>"}`. The GPU
picker can override it for a submission. A missing pinned GPU reports an error;
it does not silently choose another device. The worker uses dynamic CPU/GPU
offloading, and shares the existing video execution lease to avoid overlapping
model loads. No existing video defaults are changed.

## LUSTIFY SDXL Inpainting

Use one source image and paint the area to replace, or upload a black/white mask.
White is replaced; black is preserved. Output outside the mask is composited
from the resized source. **Text to image** uses the same checkpoint with a full
mask. The initial settings are 1024×1024, 30 steps, CFG 7, DPM++ 2M / Karras.
These are editable local starting settings, not claimed publisher settings.

The checkpoint contains the SDXL model, both CLIP text encoders, and VAE. No
additional LoRA is needed. The downloaded Diffusers folder is also retained.

Source: [andro-flock/LUSTIFY-SDXL-NSFW-checkpoint-v2-0-INPAINTING](https://huggingface.co/andro-flock/LUSTIFY-SDXL-NSFW-checkpoint-v2-0-INPAINTING).

## Qwen Rapid Image Edit

Uses **v23 NSFW** from
[Phr00t/Qwen-Image-Edit-Rapid-AIO](https://huggingface.co/Phr00t/Qwen-Image-Edit-Rapid-AIO),
revision `691024f438640508f8aa86414863fc15edfb8a84`. The all-in-one checkpoint
already contains its Qwen encoder, VAE, acceleration, and adapter merge.
It handles ordinary generation/editing as well as the selected model variant's
other capabilities. No extra acceleration LoRA is applied.

Defaults follow the v23 recommendation: 4 steps, CFG 1, Euler ancestral / Beta.
Edit supports one to four references. Text to image requires none. Conditioning
adapts the publisher's Apache-2.0 `fixed-textencode-node/nodes_qwen.v2.py`: reference
latents use the chosen output size and the vision input uses the 384² scaling
rule. This addresses the publisher's documented crop/scale issue without
replacing shared runtime nodes.

Only `v23/Qwen-Rapid-AIO-NSFW-v23.safetensors` is required. Downloading the entire
repository also downloads all historical checkpoints, over 1.5 TB.

## SNOFS Krea 2 Turbo

Uses the exact [Civitai model version 3333138, file 3219464](https://civitai.com/models/2416142?modelVersionId=3333138):
`snofs_krea_v1_4_TURBO_int8_convrot.safetensors`. It already contains the SNOFS
adapter merge and Turbo weights. No separate SNOFS or Turbo LoRA is required.
Defaults are 8 steps, CFG 1, Euler / Simple.

Companion files:

- `DreamFast/Qwen3-VL-4b-Heretic-ComfyUI/qwen3-vl-4b-heretic_int8.safetensors`,
  revision `c5bd34e940564e4b5b64286694d72f47f429c126`.
- `Comfy-Org/Krea-2/vae/qwen_image_vae.safetensors`, revision
  `588feaf955b08b9ac4723eeb751752ab045dba71`.

The [Heretic encoder publisher](https://huggingface.co/DreamFast/Qwen3-VL-4b-Heretic-ComfyUI)
explicitly documents Krea 2 compatibility. It is loaded as Krea2, which uses
twelve hidden-layer taps from Qwen3-VL-4B. The H3 32B encoder is not substituted.
The standard Qwen encoder inside Qwen Rapid AIO remains bundled and unchanged;
the separate Krea encoder is Heretic as requested.

## Implementation

The application exposes `/api/image-workflows/inventory` and owner-scoped jobs.
It imports no GPU runtime. A private worker invokes the already installed,
pinned inference library at `runtimes/minimax-h3/ComfyUI` directly. No ComfyUI
server or arbitrary node graph is started. Only these reviewed recipes execute.
Client uploads are bounded images, and model paths come from installed library
roots rather than request fields. Submission IDs avoid duplicate renders after
a lost response. PNG output includes the submitted settings.

The native iPhone/iPad client also exposes the three recipes in Cookbook →
Launch. For inpainting, choose a photo from Photos or Files, tap **Paint area**,
and paint over what should change. Brush size, erase, undo, and clear are
available over the visible photo. **Use selection** saves the highlighted area;
the app creates the mask automatically. Cancel keeps the previous selection,
and removing the source clears it. Existing masks can be imported under
**Advanced**. The client preserves drafts across navigation and previews/shares
authenticated results. Painting is also available in the web client.

On this WSL installation, Krea and Qwen Rapid have checksum-verified copies in
the Linux models directory. Their original E: copies are retained. Direct
streaming from the Windows network drive failed with pread errno 12 inside
the model loader; the Linux copies avoid that filesystem path. Copy receipts
are in outputs/image-workflow-setup/linux-model-copies.json.

## Verification on this installation

On November 8, 2026, all three recipes completed real 1024×1024 renders on the
RTX 4090 with their default sampler and step settings. Outputs were inspected;
the inpainting check also preserved pixels outside its mask exactly.

| Recipe | Steps | Runtime after acquiring the GPU lease |
| --- | --- | --- |
| LUSTIFY inpainting | 30 | 81.93 seconds |
| SNOFS Krea, Heretic encoder | 8 | 19.66 seconds |
| Qwen Rapid image edit | 4 | 22.81 seconds |

These timings describe the individual verification runs, not a performance
guarantee. The jobs waited for an active video to finish before acquiring the
shared execution lease. Settings, paths, output hashes and logs are recorded in
outputs/image-workflow-setup/runtime-verification.json and its adjacent logs.
