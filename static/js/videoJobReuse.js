import { serverVideoInput } from './videoInputs.js';

export async function openOriginalVideoDraft(input, anchor) {
  if (!/^\/api\/video\/(h3|bfs)\/jobs\/[a-f0-9]{32}\/input-videos\/(source_video|reference_videos)\/\d+\/reuse\?revision=\d+$/.test(input.reuse_url || '')) {
    throw new Error('Refresh this job to load its original video.');
  }
  const response = await fetch(input.reuse_url, { credentials: 'same-origin' });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not reuse this original video.');
  if (!['h3','bfs'].includes(result.family) || !result.config || !result.uploads) throw new Error('The server returned an invalid video draft.');
  result.uploads = Object.fromEntries(Object.entries(result.uploads).map(([field, files]) => [field, files.map(serverVideoInput)]));
  if (result.family === 'bfs') {
    const { showBfsVideo } = await import('./bfsVideo.js');
    return showBfsVideo({ initialDraft: result }, anchor);
  }
  if (result.config.recipe === 'seed_hunter') {
    const { showH3SeedHunter } = await import('./h3SeedHunter.js');
    return showH3SeedHunter({ scout: result.config.seed_hunter?.stage === 'scout', initialDraft: result }, anchor);
  }
  const { showH3Video } = await import('./h3Video.js');
  return showH3Video({ recipe: result.config.recipe || '', initialDraft: result }, anchor);
}
