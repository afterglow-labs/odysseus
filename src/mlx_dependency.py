"""Choose MLX-LM's backend using only the selected Python's metadata.

This standalone source also runs in the dependency probe on remote servers.
It must not import MLX, PyTorch, or any other GPU runtime.
"""

from importlib import metadata as _mlx_metadata
import re as _mlx_re


def mlx_lm_recipe(platform: str, *, version_fn=None) -> dict:
    """Preserve an installed backend; otherwise match CUDA or use upstream's 12.

    A catalogue without target metadata can pass a function returning ``None``.
    The selected interpreter supplies authoritative metadata before a check or
    install is offered. Plain mlx-lm installs its backend automatically on Mac,
    but current Linux releases require an explicit extra.
    """
    if not str(platform).lower().startswith("linux"):
        return {"pip": "mlx-lm"}
    version_fn = version_fn or _mlx_metadata.version

    def version(name):
        try:
            return version_fn(name)
        except _mlx_metadata.PackageNotFoundError:
            return None

    backend = None
    reason = ""
    for extra, distribution in (
        ("cuda13", "mlx-cuda-13"), ("cuda12", "mlx-cuda-12"), ("cpu", "mlx-cpu"),
    ):
        if version(distribution):
            backend = extra
            reason = f"Preserves the installed {distribution} backend."
            break
    if backend is None:
        for name in ("cuda-toolkit", "nvidia-cuda-runtime"):
            installed = version(name)
            match = _mlx_re.match(r"^(12|13)(?:\.|$)", str(installed or ""))
            if match:
                backend = "cuda" + match.group(1)
                reason = f"Matches {name} {installed} in this Python environment."
                break
    if backend is None:
        for major in (13, 12):
            if any(version(name) for name in (
                f"nvidia-cuda-runtime-cu{major}", f"nvidia-cublas-cu{major}",
            )):
                backend = f"cuda{major}"
                reason = f"Matches the installed CUDA {major} Python runtime packages."
                break
    if backend is None:
        backend = "cuda12"
        reason = "No backend was detected; uses MLX's default Linux CUDA 12 recipe."
    requirements = {
        "cuda13": "Requires a compatible NVIDIA GPU, driver 580 or newer, and glibc 2.35 or newer.",
        "cuda12": "Requires a compatible NVIDIA GPU, driver 550.54.14 or newer, and glibc 2.35 or newer.",
        "cpu": "Uses the CPU backend; requires glibc 2.35 or newer.",
    }
    return {
        "pip": f"mlx-lm[{backend}]>=0.32.2",
        "mlx_backend": backend,
        "install_hint": f"{reason} {requirements[backend]}",
    }
