// An explicit account preference can clear a troubleshooting override once.
// It is not a new application default and never touches submitted requests.
export function applyVideoMemoryReset(saved, reset, storageKey, storage = localStorage) {
  if (!reset?.revision || reset.low_vram_chunks !== 0) return false;
  const marker = storageKey + ':memory-reset';
  if (storage.getItem(marker) === reset.revision) return false;
  for (const name of ['config', 'final_config']) {
    if (saved[name]?.seed_hunter) saved[name].seed_hunter.low_vram_chunks = 0;
  }
  storage.setItem(storageKey, JSON.stringify(saved));
  storage.setItem(marker, reset.revision);
  return true;
}
