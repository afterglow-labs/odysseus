"""Compatibility helpers for optional third-party dependencies."""

from __future__ import annotations

import importlib
import sys
import types


def patch_realesrgan_torchvision_compat() -> None:
    """Restore the torchvision import path expected by BasicSR/Real-ESRGAN."""
    module_name = "torchvision.transforms.functional_tensor"
    if module_name in sys.modules:
        return
    try:
        from torchvision.transforms import functional
    except Exception:
        return

    rgb_to_grayscale = getattr(functional, "rgb_to_grayscale", None)
    if rgb_to_grayscale is None:
        return

    shim = types.ModuleType(module_name)
    shim.rgb_to_grayscale = rgb_to_grayscale
    shim.__getattr__ = lambda name: getattr(functional, name)
    sys.modules[module_name] = shim


def prepare_optional_dependency_import(name: str) -> None:
    """Apply known import-time compatibility shims before probing a package."""
    if name == "realesrgan":
        # BasicSR imports distutils.version, removed from Python 3.12+. Import
        # its provider explicitly: a live install may not have run setuptools'
        # startup .pth hook in this already-running app process.
        importlib.import_module("setuptools")
        patch_realesrgan_torchvision_compat()
