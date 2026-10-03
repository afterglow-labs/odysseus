"""Shared optional-feature definitions for checks and installation plans.

Keep import/distribution aliases explicit: a feature label or Git URL is not
necessarily the name that Python imports or package metadata records.
"""

from copy import deepcopy
import shlex

DISTRIBUTION_ALIASES = {"krea_diffusers": "diffusers", "boogu_image_mlx": "boogu-image-mlx"}
IMPORT_ALIASES = {"krea_diffusers": "diffusers"}

DEPENDENCIES = [
    # ── System ── OS binaries, not pip packages
    {
        "name": "tmux",
        "pip": "",
        "desc": "Required for Linux/Termux Cookbook background downloads and serves",
        "category": "System",
        "target": "remote",
        "kind": "system",
        "install_hint": "Run Cookbook server setup, or install tmux with apt/pacman/dnf/apk/zypper.",
    },
    {
        "name": "docker",
        "pip": "",
        "desc": "Required only for Docker-backed launch commands",
        "category": "System",
        "target": "remote",
        "kind": "system",
        "install_hint": "Install Docker on the selected server and allow this user to run docker.",
    },
    # Note: cmake / gcc / git are not separate dependency rows —
    # they're declared as `system_prereqs` on llama_cpp (and any
    # other engine that compiles from source) so they appear as
    # an inline status note on that engine's row instead of
    # cluttering the panel with raw OS package names that aren't
    # meaningful product-level dependencies on their own.
    # ── LLM ── installs on GPU servers for model serving/downloading
    {
        "name": "hf_transfer",
        "pip": "hf_transfer",
        "desc": "Fast model downloads from HuggingFace",
        "category": "Tools",
        "target": "remote",
    },
    {
        "name": "llama_cpp",
        "pip": "llama-cpp-python[server]",
        "desc": "Great for single-GPU or CPU inference with GGUF models",
        "category": "LLM",
        "target": "remote",
        # Build-toolchain prereqs. Cookbook's launch bootstrap
        # compiles llama-server from source when no prebuilt
        # binary is present; without these the build aborts
        # with `cmake: command not found`. Surfaced inline on
        # this row so the user doesn't have to chase three
        # separate OS-package rows.
        "system_prereqs": ["cmake", "g++", "git"],
    },
    {
        "name": "sglang",
        "pip": "sglang[all]",
        "desc": "Serve HF safetensors models via SGLang",
        "category": "LLM",
        "target": "remote",
    },
    {
        "name": "vllm",
        "pip": "vllm",
        "desc": "Great for high-throughput multi-GPU inference",
        "category": "LLM",
        "target": "remote",
    },
    {
        "name": "mlx_lm",
        "pip": "mlx-lm",
        "desc": "Serve MLX-format models on Apple Silicon Macs",
        "category": "LLM",
        "target": "remote",
    },
    {
        "name": "APFEL",
        "pip": "",
        "desc": "OpenAI-compatible API for Apple Foundational Models on Apple Silicon",
        "category": "LLM",
        "target": "local",
        "kind": "system",
        "install_cmd": "brew install apfel",
        "update_cmd": "brew upgrade apfel",
        "install_hint": "Requires a native Apple Silicon Mac with Apple Foundational Models support. Installable via Homebrew on supported Macs.",
    },
    # ── Image ── editor + diffusion model serving
    {
        "name": "diffusers",
        "pip": "diffusers[torch] torchvision transformers accelerate scipy python-multipart",
        "desc": "Image generation/editing pipelines with PyTorch and Diffusers",
        "category": "Image",
        "target": "remote",
    },
    {
        "name": "krea_diffusers",
        "pip": "git+https://github.com/huggingface/diffusers.git torchvision transformers accelerate scipy python-multipart",
        "desc": "Latest Diffusers from Git for newly released image pipelines",
        "category": "Image",
        "target": "remote",
    },
    {
        "name": "mflux",
        "pip": "mflux fastapi uvicorn python-multipart",
        "desc": "MLX image generation runtime for Apple Silicon models like Qwen Image",
        "category": "Image",
        "target": "remote",
    },
    {
        "name": "boogu_image_mlx",
        "pip": "git+https://github.com/xocialize/boogu-image-mlx.git fastapi uvicorn python-multipart pillow",
        "desc": "MLX image generation pipeline for Boogu Image models on Apple Silicon",
        "category": "Image",
        "target": "remote",
    },
    {
        "name": "mlx_lama_swift",
        "pip": "",
        "desc": "Swift MLX runtime for LaMa / MI-GAN inpainting and object removal",
        "category": "Image",
        "target": "remote",
        "install_hint": "Build an Odysseus-compatible mlx-lama-swift bridge on the selected Apple Silicon Mac and put odysseus-mlx-inpaint or mlx-lama-serve on PATH. Upstream currently ships Swift libraries plus smoke executables, not a stable image-edit CLI.",
    },
    {
        "name": "mlx_ddcolor_swift",
        "pip": "",
        "desc": "Swift MLX runtime for DDColor automatic image colorization",
        "category": "Image",
        "target": "remote",
        "install_hint": "Build an Odysseus-compatible mlx-ddcolor-swift bridge on the selected Apple Silicon Mac and put odysseus-mlx-colorize or mlx-ddcolor-serve on PATH. Upstream currently ships Swift libraries plus smoke executables, not a stable colorize CLI.",
    },
    {
        "name": "mlx_vlm",
        "pip": "mlx-vlm",
        "desc": "MLX-VLM backbone used by HiDream image models on Apple Silicon",
        "category": "Image",
        "target": "remote",
    },
    {
        "name": "transformers",
        "pip": "transformers",
        "desc": "Hugging Face model components used by SD/Flux pipelines and image tools",
        "category": "Image",
        "target": "remote",
    },
    {
        "name": "sam_mask",
        "pip": "torch torchvision transformers accelerate pillow",
        "desc": "Neutral click/box/object segmentation masks for the image editor",
        "category": "Image",
        "target": "local",
    },
    {
        "name": "rembg",
        "pip": "rembg[gpu]",
        "desc": "AI background removal for image editor",
        "category": "Image",
        "target": "local",
    },
    {
        "name": "realesrgan",
        "pip": "realesrgan",
        "desc": "AI denoise + upscale (Real-ESRGAN). Used by editor's Denoise and Upscale tools.",
        "category": "Image",
        "target": "local",
    },
    # ── Tools ──
    {
        "name": "playwright",
        "pip": "playwright",
        "desc": "Browser automation for web tools",
        "category": "Tools",
        "target": "local",
    },
]


def dependency_catalog(*, local_platform="", target_platform=""):
    """Return independent plans; app tools always use the local platform."""
    packages = deepcopy(DEPENDENCIES)
    for pkg in packages:
        platform = local_platform if pkg.get("target") == "local" else target_platform
        platform = platform.lower()
        if pkg["name"] == "rembg" and platform in {"darwin", "macos", "mac"}:
            pkg["pip"] = "rembg[cpu]"
        if pkg["name"] in {"vllm", "sglang"} and platform in {"darwin", "macos", "mac", "win32", "windows", "win"}:
            pkg["install_supported"] = False
            pkg["install_hint"] = "This automatic install recipe requires a Linux model server. Select a Linux server, or use llama.cpp / MLX for native Mac inference."
        pkg.setdefault("install_supported", True)
    return packages


def requirement_specs(pkg):
    """The same pip inputs drive prerequisite checks and install buttons."""
    if pkg.get("kind") == "system" or not pkg.get("pip"):
        return []
    specs = shlex.split(pkg["pip"])
    if specs and specs[0].startswith("git+"):
        specs[0] = DISTRIBUTION_ALIASES.get(pkg["name"], pkg["name"].replace("_", "-"))
    return specs
