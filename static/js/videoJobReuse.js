import { serverVideoInput } from './videoInputs.js';

export async function openOriginalVideoDraft(input, anchor) {
  if (!/^\/api\/video\/(h3|bfs)\/jobs\/[a-f0-9]{32}\/input-videos\/(source_video|reference_videos)\/\d+\/reuse\?revision=\d+$/.test(input.reuse_url || '')) {
    throw new Error('Refresh this job to load its original video.');
  }
  const response = await fetch(input.reuse_url, { credentials: 'same-origin' });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not reuse this original video.');
  return openVideoDraft(result, anchor);
}

export function createExtendVideoButton(jobId) {
  const node = document.createElement('button'); node.type = 'button'; node.className = 'memory-toolbar-btn';
  node.textContent = 'Extend this video';
  const error = document.createElement('span'); error.className = 'h3-video-error'; error.setAttribute('role', 'status');
  const container = document.createElement('span'); container.append(node, error);
  node.onclick = async () => {
    if (node.disabled) return;
    node.disabled = true; node.textContent = 'Opening continuation…'; error.textContent = '';
    try {
      if (!/^[a-f0-9]{32}$/.test(jobId)) throw new Error('Refresh this video job before extending it.');
      const response = await fetch(`/api/video/h3/jobs/${jobId}/extend`, { credentials: 'same-origin' });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not prepare video continuation.');
      await openVideoDraft(result, node);
    } catch (failure) { error.textContent = failure.message; }
    finally { node.disabled = false; node.textContent = 'Extend this video'; }
  };
  return container;
}

function openVideoDraft(result, anchor) {
  if (!['h3','bfs'].includes(result.family) || !result.config || !result.uploads) throw new Error('The server returned an invalid video draft.');
  result.uploads = Object.fromEntries(Object.entries(result.uploads).map(([field, files]) => [field, files.map(serverVideoInput)]));
  if (result.family === 'bfs') {
    return import('./bfsVideo.js').then(({ showBfsVideo }) => showBfsVideo({ initialDraft: result }, anchor));
  }
  if (result.config.recipe === 'seed_hunter') {
    return import('./h3SeedHunter.js').then(({ showH3SeedHunter }) => showH3SeedHunter({ scout: result.config.seed_hunter?.stage === 'scout', initialDraft: result }, anchor));
  }
  return import('./h3Video.js').then(({ showH3Video }) => showH3Video({ recipe: result.config.recipe || '', initialDraft: result }, anchor));
}
