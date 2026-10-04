import sys
import types
import subprocess
from pathlib import Path

from src.optional_deps import (
    patch_realesrgan_torchvision_compat,
    prepare_optional_dependency_import,
)


def test_realesrgan_preparation_restores_distutils_without_startup_pth_hook():
    # Simulate installing setuptools after the long-running app started.
    # Exercise its real compatibility module in an isolated interpreter.
    script = '''
import sys
import _distutils_hack
sys.meta_path[:] = [finder for finder in sys.meta_path
                   if not isinstance(finder, _distutils_hack.DistutilsMetaFinder)]
for name in list(sys.modules):
    if name == 'distutils' or name.startswith('distutils.'):
        del sys.modules[name]
import src.optional_deps as deps
deps.patch_realesrgan_torchvision_compat = lambda: None
deps.prepare_optional_dependency_import('realesrgan')
from distutils.version import LooseVersion
assert LooseVersion('1.2') < LooseVersion('1.3')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_realesrgan_patch_restores_removed_functional_tensor_module(monkeypatch):
    for name in list(sys.modules):
        if name.startswith("torchvision"):
            monkeypatch.delitem(sys.modules, name, raising=False)

    sentinel = object()
    torchvision = types.ModuleType("torchvision")
    transforms = types.ModuleType("torchvision.transforms")
    functional = types.ModuleType("torchvision.transforms.functional")
    functional.rgb_to_grayscale = sentinel
    transforms.functional = functional
    torchvision.transforms = transforms
    monkeypatch.setitem(sys.modules, "torchvision", torchvision)
    monkeypatch.setitem(sys.modules, "torchvision.transforms", transforms)
    monkeypatch.setitem(sys.modules, "torchvision.transforms.functional", functional)

    patch_realesrgan_torchvision_compat()

    shim = sys.modules["torchvision.transforms.functional_tensor"]
    assert shim.rgb_to_grayscale is sentinel
    assert shim.rgb_to_grayscale is functional.rgb_to_grayscale


def test_prepare_optional_dependency_import_scopes_patch_to_realesrgan(monkeypatch):
    import src.optional_deps as optional_deps

    calls = []
    monkeypatch.setattr(
        optional_deps,
        "patch_realesrgan_torchvision_compat",
        lambda: calls.append("patched"),
    )

    prepare_optional_dependency_import("diffusers")
    assert calls == []

    prepare_optional_dependency_import("realesrgan")
    assert calls == ["patched"]
