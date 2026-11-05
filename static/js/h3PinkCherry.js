import { showH3Video } from './h3Video.js';

export const showPinkCherry = (options = {}, anchor) => showH3Video({ ...options, recipe: 'pinkcherry' }, anchor);
export function createPinkCherryControls({ form, onPreset }) {
  const node = document.createElement('section'); node.className = 'h3-director-controls';
  const title = document.createElement('h3'); title.textContent = 'PinkCherry presets';
  const buttons = document.createElement('div'); buttons.className = 'h3-video-toolbar';
  const help = document.createElement('p'); help.className = 'h3-video-muted';
  help.textContent = 'Your PinkCherry JSON: hybrid v1.8, FL Turbo v1.0 at strength 1, and Heretic Qwen. Default: 12 steps, 12.25 seconds, 672 × 1216. Both presets generate the final clip directly.';
  node.append(title, buttons, help); form.prepend(node);
  return {
    node, get: () => ({ recipe: 'pinkcherry' }), set: () => {}, hasPrompt: () => false,
    refreshReferences: () => {}, removeReference: () => {},
    presets: capability => {
      buttons.replaceChildren();
      for (const preset of capability?.presets || []) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'memory-toolbar-btn';
        button.textContent = preset.name; button.disabled = !preset.ready; button.onclick = () => onPreset(preset); buttons.append(button);
        if (!preset.ready) { const missing = document.createElement('p'); missing.textContent = `Needs: ${preset.missing.join(', ')}`; buttons.append(missing); }
      }
    },
  };
}
