"""Release caches in the running Odysseus process, without touching other apps."""

import gc
import sys
import threading

_clear_lock = threading.Lock()


def clear_vram(*, unload_models=False):
    with _clear_lock:
        return _clear_vram(unload_models=unload_models)


def _clear_vram(*, unload_models):
    unloaded, errors, runtimes = [], [], []
    released = 0
    if unload_models:
        gallery = sys.modules.get('routes.gallery.gallery_routes')
        for name in ('_SAM_STATE', '_GROUNDING_STATE'):
            cache = getattr(gallery, name, None)
            if isinstance(cache, dict):
                unloaded.extend(cache)
                # In-flight editor calls hold their own references. Never move
                # their tensors to CPU or invalidate an active model in place.
                cache.clear()

    # Importing a runtime here could create a fresh GPU context just to clear it.
    torch = sys.modules.get('torch')
    cuda = getattr(torch, 'cuda', None)
    cuda_before = {}
    if cuda is not None and cuda.is_initialized():
        try:
            cuda_before = {i: cuda.memory_reserved(i) for i in range(cuda.device_count())}
        except Exception as exc:
            errors.append(f'CUDA cache: {exc}')
    gc.collect()
    for index, before in cuda_before.items():
        if not before:
            continue
        try:
            with cuda.device(index):
                cuda.empty_cache()
            released += max(0, before - cuda.memory_reserved(index))
            runtimes.append(f'CUDA GPU {index}')
        except Exception as exc:
            errors.append(f'CUDA GPU {index}: {exc}')

    mps = getattr(torch, 'mps', None)
    mps_backend = getattr(getattr(torch, 'backends', None), 'mps', None)
    if mps is not None and mps_backend is not None and mps_backend.is_available():
        try:
            before = mps.driver_allocated_memory()
            if before:
                mps.empty_cache()
                released += max(0, before - mps.driver_allocated_memory())
                runtimes.append('Metal')
        except Exception as exc:
            errors.append(f'Metal cache: {exc}')

    mlx = sys.modules.get('mlx.core')
    if mlx is not None:
        try:
            before = mlx.get_cache_memory()
            mlx.clear_cache()
            released += max(0, before - mlx.get_cache_memory())
            runtimes.append('MLX')
        except Exception as exc:
            errors.append(f'MLX cache: {exc}')

    released_mb = round(released / (1024 ** 2), 1)
    message = f'Released {released_mb:g} MB of unused GPU cache.' if runtimes else 'No active GPU cache to release in Odysseus.'
    if unloaded:
        message += f' Unloaded {len(unloaded)} cached editor models. Active editor operations release their remaining memory when they finish.'
    if not unload_models:
        message += ' Loaded models remain in memory.'
    return {'ok': not errors, 'message': message, 'released_mb': released_mb,
            'unloaded_models': unloaded, 'runtimes': runtimes, 'errors': errors}
