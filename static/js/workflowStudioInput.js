import { serverVideoInput } from './videoInputs.js';

// Describe one source without opening it. The Studio owns media playback and
// object URLs; server video requests start only after its Play button is used.
export function workflowStudioInputPreview(candidates = [], family = 'h3', origin = globalThis.location?.origin || 'http://localhost') {
  if (!['h3', 'bfs'].includes(family)) return null;
  const prefix = `/api/video/${family}/`;
  for (const input of candidates.filter(Boolean)) {
    const label = String(input.name || 'Source preview');
    const type = /^image\//.test(input.type || '') || /\.(png|jpe?g|webp|gif|avif)$/i.test(label) ? 'image'
      : /^audio\//.test(input.type || '') || /\.(wav|mp3|m4a|aac|flac|ogg|opus)$/i.test(label) ? 'audio' : 'video';
    if (typeof File !== 'undefined' && input instanceof File) return { file: input, label, type };
    if (input.serverFile) {
      try {
        const descriptor = serverVideoInput(input.serverFile).serverFile;
        return { url: `${prefix}server-files/video?${new URLSearchParams(descriptor)}`, label, type: 'video' };
      } catch { continue; }
    }
    if (input.preview_url) {
      try {
        const url = new URL(input.preview_url, origin);
        if (url.origin === origin && url.pathname.startsWith(prefix) && !url.username && !url.password)
          return { url: url.href, label, type };
      } catch { /* Invalid or external preview descriptors are not opened. */ }
    }
  }
  return null;
}
