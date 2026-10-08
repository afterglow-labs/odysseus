// GPU button indexes belong to the host's probe, not CUDA's device ordering.
export function gpuVisibility(selection, probe, target) {
  if (!probe?.byIdx || probe.host !== (target?.host || '')
      || probe.serverKey !== (target?.serverKey || '')) return null;
  const ids = String(selection || '').split(',').filter(Boolean).map(id => {
    const gpu = probe.byIdx.get(Number(id));
    return probe.backend === 'cuda' && /^GPU-[a-f0-9-]+$/i.test(gpu?.uuid || '') ? gpu.uuid : id;
  });
  return { ids: ids.join(','), backend: probe.backend };
}

export function gpuButtonLabel(gpu) {
  return `${gpu.index} · ${String(gpu.name || 'GPU').replace(/^NVIDIA\s+(?:GeForce\s+)?/i, '')}`;
}
