// static/js/cookbookProgressSignal.js
/**
 * Fingerprint of reported download/install progress. Prefer downloaded bytes;
 * otherwise include the output tail so resolver/build messages count as updates.
 * An unchanged signal only means that the log is quiet. Transfers and cached
 * copies can continue silently, so this must never justify killing a process.
 */
export function computeProgressSignal(bytes, dlAgg, lastPct, snapshot) {
  if (bytes) return bytes;
  const base = dlAgg != null ? String(dlAgg) : (lastPct || '0');
  // No byte counter → use the output tail so a build/resolve phase that emits new
  // lines counts as progress instead of a false stall (#1568).
  return base + '|' + String(snapshot || '').slice(-300);
}
