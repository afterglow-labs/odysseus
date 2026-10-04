import uiModule from './ui.js';

export function gpuProcesses(data) {
  const unique = new Map();
  for (const process of [...(data.unassigned_processes || []), ...(data.gpus || []).flatMap(g => g.processes || [])]) {
    if (process.pid >= 100) unique.set(process.pid, process);
  }
  return [...unique.values()];
}

export async function clearGpuMemory({ host = '', sshPort = '', button, onCleared } = {}) {
  if (button?.disabled) return;
  if (button) button.disabled = true;
  const target = host || 'Local Odysseus server';
  const query = new URLSearchParams();
  if (host) query.set('host', host);
  if (sshPort) query.set('ssh_port', sshPort);
  const probe = async () => {
    const response = await fetch('/api/cookbook/gpus?' + query, { credentials: 'same-origin', cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || data.detail || 'GPU probe failed');
    return data;
  };
  const stop = async (processes, signal) => Promise.all(processes.map(async p => {
    const response = await fetch('/api/cookbook/kill-pid', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pid: p.pid, signal, host: host || null, ssh_port: sshPort || null, start_time: p.start_time || null, gpu_only: true }),
    });
    const result = await response.json();
    return { ...result, ok: response.ok && result.ok };
  }));
  try {
    const before = await probe();
    const processes = gpuProcesses(before);
    if (!processes.length) {
      uiModule.showToast('No model GPU processes found on ' + target, 5000);
      return;
    }
    const summary = processes.map(p => `${p.name} (PID ${p.pid})`).join('\n');
    if (!await window.styledConfirm(`Clear VRAM on ${target}? This stops these GPU processes:\n\n${summary}`, { confirmText: 'Stop and clear VRAM', danger: true })) return;
    const results = await stop(processes, 'TERM');
    await new Promise(resolve => setTimeout(resolve, 1500));
    let after = await probe();
    const survivors = gpuProcesses(after).filter(p => processes.some(old => old.pid === p.pid && (!old.start_time || old.start_time === p.start_time)));
    if (survivors.length) {
      if (!await window.styledConfirm(`${survivors.length} GPU process(es) are still running:\n\n${survivors.map(p => `${p.name} (PID ${p.pid})`).join('\n')}\n\nForce stop them to release VRAM?`, { confirmText: 'Force stop', danger: true })) return;
      await stop(survivors, 'KILL');
      await new Promise(resolve => setTimeout(resolve, 800));
      after = await probe();
    }
    const remaining = gpuProcesses(after).filter(p => processes.some(old => old.pid === p.pid && (!old.start_time || old.start_time === p.start_time)));
    const freed = Math.max(0, (after.gpus || []).reduce((n, g) => n + g.free_mb, 0) - (before.gpus || []).reduce((n, g) => n + g.free_mb, 0));
    if (remaining.length) uiModule.showToast(`Could not stop ${remaining.length} GPU process(es). ${results.find(r => r.error)?.error || 'Check process permissions.'}`, 7000);
    else uiModule.showToast(`Stopped ${processes.length} GPU process(es). VRAM released: ${(freed / 1024).toFixed(1)} GB.`, 6000);
    if (onCleared) await onCleared();
  } catch (error) {
    uiModule.showToast('Clear VRAM failed: ' + error.message, 6000);
  } finally {
    if (button) button.disabled = false;
  }
}
