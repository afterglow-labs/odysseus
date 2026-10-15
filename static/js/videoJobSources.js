import { createVideoJobMedia, releaseVideoJobMedia } from './videoJobMedia.js';

const make = (tag, className, text) => {
  const node = document.createElement(tag); node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const timestamp = seconds => {
  const value = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(value / 60)}:${(value % 60).toFixed(2).padStart(5, '0')}`;
};

// Each job owns its source players. Polling only changes labels unless an
// attachment's URL/revision changes; media bytes are requested on Play only.
export function createVideoJobSources() {
  const node = make('section', 'h3-video-originals');
  node.setAttribute('aria-label', 'Original input videos'); node.hidden = true;
  const entries = new Map();
  function update(inputVideos) {
    const videos = Array.isArray(inputVideos) ? inputVideos.filter(item => item && typeof item.url === 'string' && item.url) : [];
    const ids = new Set(videos.map(item => item.id || `${item.field}:${item.index}`));
    for (const [id, entry] of entries) if (!ids.has(id)) {
      releaseVideoJobMedia(entry.card); entry.card.remove(); entries.delete(id);
    }
    for (const [index, item] of videos.entries()) {
      const id = item.id || `${item.field}:${item.index}`, version = `${item.url}\n${item.revision ?? ''}`;
      let entry = entries.get(id);
      if (entry && entry.version !== version) {
        releaseVideoJobMedia(entry.card); entry.card.remove(); entries.delete(id); entry = null;
      }
      if (!entry) {
        const card = make('article', 'h3-video-original'), heading = make('h5', ''), name = make('p', 'h3-video-original-name'), context = make('p', 'h3-video-muted');
        const player = createVideoJobMedia(item.url, { label: item.playback === 'selected_section' ? 'Play selected source section' : 'Play original video' });
        card.dataset.inputVideoId = id; card.append(heading, name, context, player.node);
        entry = { card, heading, name, context, version }; entries.set(id, entry);
      }
      const section = item.playback === 'selected_section';
      entry.heading.textContent = section ? 'Selected source section' : item.field === 'source_video' ? 'Original source video' : 'Original video';
      entry.name.textContent = item.name || 'Source video';
      const role = item.field === 'reference_videos' ? `Reference <Video ${Number(item.index) + 1}>` : 'Source video';
      const range = section ? `${timestamp(item.start_seconds)}${Number.isFinite(item.duration_seconds) ? `–${timestamp(Number(item.start_seconds) + item.duration_seconds)}` : ' onward'}` : '';
      entry.context.textContent = [role, range].filter(Boolean).join(' · ');
      if (node.children[index] !== entry.card) node.insertBefore(entry.card, node.children[index] || null);
    }
    node.hidden = videos.length === 0;
  }
  return { node, update };
}
