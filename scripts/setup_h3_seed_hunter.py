#!/usr/bin/env python3
"""Install pinned Seed Hunter extras in Odysseus's private runtime and models.

Leaves the existing Comfy inference core and user model selections untouched.
Downloads about 1.1 GB of exact upscaler, DMAD adapter and RIFE weights to Linux.
"""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
CODES = {
    "latent-upscaler": ("https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler.git", "40316cf008b2fd8663263270669eb4da23f89d2c"),
    "refmods": ("https://github.com/Luisacaotica/ComfyUI-MiniMaxH3Mod.git", "f9462081e28794389b5a6c5067eb327412ad8ee7"),
    "kjnodes": ("https://github.com/kijai/ComfyUI-KJNodes.git", "d7641533d6634eb05f8570c980fce8fc8528dc7b"),
}
WEIGHTS = [
    ("LBH-123-AI/Minimax_h3_latent_Upscaler", "3f941d5d182014dd5c0a5e16330420ee2d4aa0c6",
     "minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors",
     "043e5a48e161610ef6c3ea974645220354d06fa618abca15f76d084812eb55c2"),
    ("Kijai/MiniMax-H3-experimental", "d8023be02fefbb3633b0cd335c3879f91177299d",
     "loras/minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors",
     "ddfdd94f4c9a0f32bf3649bb8e98ca20db572e4db6bcd9cca546a5b33ea32d11"),
    ("hzwer/RIFE", "01fdc7e97404120c243c3ea7b427046e5dc7643e", "RIFEv4.26_0921.zip",
     "1fa9b9cda3d9b8c3e301359e2595960902f97bf926c08598b0e9957a3f3f760e"),
]


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


def setup():
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Run using Odysseus venv/bin/python; global Python is not modified")
    from huggingface_hub import hf_hub_download
    from safetensors.torch import save_file
    import torch
    extras = PROJECT / "runtimes/minimax-h3/seed-hunter"
    extras.mkdir(parents=True, exist_ok=True)
    for name, (repository, revision) in CODES.items():
        destination = extras / name
        if destination.exists():
            if git(destination, "rev-parse", "HEAD") != revision or git(destination, "status", "--porcelain", "--untracked-files=no"):
                raise RuntimeError(f"Existing {name} differs from the pinned revision; keeping it untouched")
        else:
            destination.mkdir()
            git(destination, "init")
            git(destination, "remote", "add", "origin", repository)
            git(destination, "fetch", "--depth", "1", "origin", revision)
            git(destination, "checkout", "--detach", "FETCH_HEAD")
    receipts = []
    for repository, revision, filename, expected in WEIGHTS:
        path = Path(hf_hub_download(repository, filename, revision=revision,
                                   local_dir=PROJECT / "models" / repository))
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise RuntimeError(f"Checksum mismatch for {filename}")
        receipts.append({"repository": repository, "revision": revision, "filename": filename,
                         "sha256": digest, "bytes": path.stat().st_size})
    archive = PROJECT / "models/hzwer/RIFE/RIFEv4.26_0921.zip"
    with zipfile.ZipFile(archive) as zipped:
        members = [item for item in zipped.infolist() if Path(item.filename).name == "flownet.pkl"]
        if len(members) != 1 or members[0].file_size > 100_000_000:
            raise RuntimeError("Unexpected official RIFE checkpoint layout")
        # Read one exact member without extracting any archive-provided path.
        import io
        state = torch.load(io.BytesIO(zipped.read(members[0])), map_location="cpu", weights_only=True)
    if not isinstance(state, dict) or not all(isinstance(k, str) and isinstance(v, torch.Tensor) for k, v in state.items()):
        raise RuntimeError("RIFE checkpoint must contain only named tensors")
    save_file({k: v.contiguous() for k, v in state.items()}, str(archive.with_name("rife-v4.26.safetensors")),
              metadata={"source": "hzwer/RIFE@" + WEIGHTS[2][1]})
    (extras / "install-receipt.json").write_text(json.dumps({"code": CODES, "weights": receipts}, indent=2) + "\n")
    print("Seed Hunter runtime extras and verified weights are ready in Odysseus's Linux folders.")


if __name__ == "__main__":
    setup()
