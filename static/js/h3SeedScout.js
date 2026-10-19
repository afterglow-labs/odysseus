// Native Seed Scout uses the original H3 form and the server's owned job state.
export const SEED_SCOUT_OPTIONS = Object.freeze({ stage: 'scout', scout_count: 3, scout_step: 3, scout_mode: 'interactive', scout_timeout: 0, scout_preview_mode: 'vae' });
const retainedDrafts = new Map();
export function seedScoutDraft(value, key = 'classic') { if (value !== undefined) retainedDrafts.set(key,value); return retainedDrafts.get(key); }
export function isSeedScoutJob(job) { return job?.seed_hunter?.stage === 'scout' || job?.config?.seed_hunter?.stage === 'scout' || !!job?.scout; }
const element = (tag, cls = '', text) => { const n = document.createElement(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n; };
export function createSeedScoutPreviews(jobId, { select, onError = () => {} }) {
  const node = element('section', 'h3-scout-previews'); node.setAttribute('aria-label', 'Seed Scout choices');
  const status = element('p', 'h3-video-muted'); status.setAttribute('role', 'status');
  const grid = element('div', 'h3-scout-grid'); node.append(status, grid);
  const cards = new Map(); let current, selecting = false;
  function update(job) {
    current = job;
    const scout = job.scout || {}, settings = job.seed_hunter || {}, count = Number(settings.scout_count) || scout.seeds?.length || 3;
    const seeds = scout.seeds?.length ? scout.seeds : Array.from({ length: count }, (_, i) => `pending-${i+1}`);
    const mode=scout.mode||settings.scout_mode||'interactive';node.hidden=mode==='final';
    for (const [seed, card] of cards) if (!seeds.includes(seed)) { card.wrap.remove(); cards.delete(seed); }
    const waiting = mode==='interactive' && job.status === 'running' && scout.status === 'waiting_for_selection' && scout.selected_seed == null;
    status.textContent = waiting ? `Choose a seed to continue from step ${scout.scout_step} of ${scout.total_steps}. Viewing a preview does not choose it.`
      : scout.selected_seed != null ? `Seed ${scout.selected_seed} selected · ${job.status === 'completed' ? 'complete' : job.phase==='upscaling_latent'?'upscaling the finished latent':job.phase==='refining_upscaled_video'?'refining the upscaled video':'continuing the selected partial latent'}.`
      : job.status==='completed' && mode==='scout' ? 'Partial previews complete. This job did not run the rest of the sampling schedule.'
      : job.status === 'failed' || job.status === 'stopped' ? 'This job has stopped. Saved previews remain available.'
      : 'Scouting partial previews. Each choice will appear here when ready.';
    for (const seed of seeds) {
      let card = cards.get(seed);
      if (!card) {
        const wrap = element('section', 'h3-scout-choice'), title = element('h5', '', typeof seed==='number'?`Seed ${seed}`:`Preview ${seed.split('-')[1]}`), placeholder = element('p', 'h3-video-muted', 'Preview pending…');
        const image = element('img', 'h3-scout-preview'); image.alt = `Animated preview for seed ${seed}`; image.hidden = true; image.loading = 'lazy';
        const choose = element('button', 'memory-toolbar-btn', 'Continue this seed'); choose.type = 'button';
        choose.onclick = async () => {
          if (selecting || choose.disabled || current?.id !== jobId) return;
          selecting = true; update(current);
          try { const result = await select(seed); if (current) update({ ...current, scout: result.scout || result }); }
          catch (error) { onError(error); }
          finally { selecting = false; if (current) update(current); }
        };
        wrap.append(title, image, placeholder, choose); grid.append(wrap); card = { wrap, image, placeholder, choose }; cards.set(seed, card);
      }
      const preview = scout.previews?.find(item => item.seed === seed);
      if (preview) {
        const url = `/api/video/h3/jobs/${encodeURIComponent(jobId)}/scout/previews/${seed}?preview=${preview.provisional?'rough':'decoded'}`;
        if (card.image.getAttribute('src') !== url) card.image.src = url;
        card.image.hidden = false; card.placeholder.hidden = true;
      }
      card.choose.hidden = !waiting && scout.selected_seed !== seed;
      card.choose.disabled = selecting || !waiting || !preview;
      card.choose.textContent = scout.selected_seed === seed ? 'Selected seed' : selecting ? 'Selecting…' : 'Continue this seed';
      card.wrap.classList.toggle('selected', scout.selected_seed === seed);
    }
  }
  return { node, update };
}
