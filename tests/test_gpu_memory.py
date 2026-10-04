from types import SimpleNamespace
from unittest.mock import Mock
import sys

from src import gpu_memory


def test_cache_clear_does_not_import_or_initialize_a_gpu_runtime(monkeypatch):
    monkeypatch.setitem(sys.modules, 'torch', None)
    monkeypatch.setitem(sys.modules, 'mlx.core', None)
    result = gpu_memory.clear_vram()
    assert result['ok']
    assert result['unloaded_models'] == []
    assert 'No active GPU cache' in result['message']


def test_cache_only_preserves_models_and_reports_released_bytes(monkeypatch):
    from contextlib import nullcontext
    cache = {'sam': object()}
    monkeypatch.setitem(sys.modules, 'routes.gallery.gallery_routes', SimpleNamespace(_SAM_STATE=cache, _GROUNDING_STATE={}))
    cuda = SimpleNamespace(is_initialized=lambda: True, device_count=lambda: 1,
        memory_reserved=Mock(side_effect=[20 * 1024**2, 5 * 1024**2]),
        device=lambda index: nullcontext(), empty_cache=Mock())
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=cuda))
    monkeypatch.setitem(sys.modules, 'mlx.core', None)
    result = gpu_memory.clear_vram()
    assert len(cache) == 1
    cuda.empty_cache.assert_called_once()
    assert result['released_mb'] == 15
    assert result['unloaded_models'] == []


def test_unload_drops_editor_cache_without_mutating_active_inference_models(monkeypatch):
    active = {'model': object()}
    sam = {'test-sam': active}
    grounding = {'test-grounding': {'model': object()}}
    monkeypatch.setitem(sys.modules, 'routes.gallery.gallery_routes', SimpleNamespace(_SAM_STATE=sam, _GROUNDING_STATE=grounding))
    monkeypatch.setitem(sys.modules, 'torch', None)
    monkeypatch.setitem(sys.modules, 'mlx.core', None)
    result = gpu_memory.clear_vram(unload_models=True)
    assert not sam and not grounding
    assert active['model'] is not None
    assert set(result['unloaded_models']) == {'test-sam', 'test-grounding'}


def test_gpu_errors_are_visible(monkeypatch):
    cuda = SimpleNamespace(is_initialized=lambda: True, device_count=Mock(side_effect=RuntimeError('GPU unavailable')))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=cuda))
    monkeypatch.setitem(sys.modules, 'mlx.core', None)
    result = gpu_memory.clear_vram()
    assert result['errors'] == ['CUDA cache: GPU unavailable']
