// Shared field help for the dynamically rendered Settings account editors.
import { registerMenuDismiss, dismissTopMenu } from '../escMenuStack.js';
import { getTooltipTrigger } from './behavior.js';

const boundModals = new WeakSet();
let active = null;
let nextId = 0;

function escapeAttribute(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

export function renderHelpHint(text, label, legacyClass = '') {
  return `<button type="button" class="settings-help-hint ${legacyClass}"`
    + ` data-help-hint="${escapeAttribute(text)}" aria-label="${escapeAttribute(`Help for ${label}`)}"`
    + ' aria-expanded="false"><span aria-hidden="true">?</span></button>';
}

export function dismissSettingsHelp() {
  active?.close();
}

function showHint(anchor, modal) {
  if (active?.anchor === anchor) {
    clearTimeout(active.leaveTimer);
    return active;
  }
  dismissSettingsHelp();

  const popup = document.createElement('div');
  popup.id = `settings-help-${++nextId}`;
  popup.className = 'settings-help-tooltip';
  popup.setAttribute('role', 'tooltip');
  popup.textContent = anchor.dataset.helpHint;
  popup.style.zIndex = String(Math.max(10000, (parseInt(getComputedStyle(modal).zIndex, 10) || 0) + 1));
  document.body.appendChild(popup);
  anchor.setAttribute('aria-describedby', popup.id);
  anchor.setAttribute('aria-expanded', 'true');

  // The app's Large text setting zooms the root to 1.25. DOM rects include
  // that scale, while fixed-position CSS coordinates still need CSS pixels.
  const scale = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  popup.style.maxWidth = `${Math.min(320, Math.max(1, window.innerWidth - 24) / scale)}px`;
  popup.style.maxHeight = `${Math.max(1, window.innerHeight - 24) / scale}px`;
  const rect = anchor.getBoundingClientRect();
  const size = popup.getBoundingClientRect();
  const left = Math.max(12, Math.min(rect.left, window.innerWidth - size.width - 12));
  const below = rect.bottom + 6;
  const top = Math.max(12, below + size.height <= window.innerHeight - 12
    ? below : rect.top - size.height - 6);
  popup.style.left = `${left / scale}px`;
  popup.style.top = `${top / scale}px`;

  const state = { anchor, popup, activated: false, keepOnFocus: false, leaveTimer: null, close: null };
  const observer = new MutationObserver(() => {
    if (!anchor.isConnected || !anchor.getClientRects().length
      || anchor.closest('.hidden, [hidden], .modal-closing')) state.close();
  });
  const onOutside = event => {
    if (!anchor.contains(event.target) && !popup.contains(event.target)) state.close();
  };
  const onScroll = event => {
    if (!popup.contains(event.target)) state.close();
  };
  const onEscape = event => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    // ui.js handles Escape in document capture, with hovered windows taking
    // priority. Run at window capture so field help closes before its editor,
    // while still respecting any newer overlay registered on the menu stack.
    if (dismissTopMenu()) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  };
  const unregister = registerMenuDismiss(() => state.close());
  state.close = () => {
    if (active !== state) return;
    active = null;
    clearTimeout(state.leaveTimer);
    unregister();
    observer.disconnect();
    document.removeEventListener('pointerdown', onOutside, true);
    document.removeEventListener('scroll', onScroll, true);
    window.removeEventListener('resize', state.close);
    window.removeEventListener('keydown', onEscape, true);
    anchor.removeAttribute('aria-describedby');
    anchor.setAttribute('aria-expanded', 'false');
    popup.remove();
  };
  active = state;
  popup.addEventListener('pointerenter', () => clearTimeout(state.leaveTimer));
  popup.addEventListener('pointerleave', event => {
    if (event.pointerType !== 'touch') scheduleLeave(anchor);
  });
  observer.observe(modal, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'style', 'hidden'] });
  document.addEventListener('pointerdown', onOutside, true);
  document.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', state.close);
  window.addEventListener('keydown', onEscape, true);
  return state;
}

function scheduleLeave(anchor) {
  if (active?.anchor !== anchor) return;
  // Mouse clicks focus buttons too. Only keyboard-opened help should stay
  // visible because of focus; pointer-opened help closes when you move away.
  if (active.keepOnFocus && document.activeElement === anchor) return;
  clearTimeout(active.leaveTimer);
  active.leaveTimer = setTimeout(dismissSettingsHelp, 150);
}

export function bindSettingsHelp(modal) {
  if (!modal || boundModals.has(modal)) return;
  boundModals.add(modal);
  document.addEventListener('odysseus:tooltip-trigger-change', dismissSettingsHelp);
  const hintFor = target => target?.closest?.('button[data-help-hint]');

  modal.addEventListener('pointerover', event => {
    const hint = hintFor(event.target);
    if (!hint || event.pointerType === 'touch' || hint.contains(event.relatedTarget)) return;
    // Re-entering an open hint cancels dismissal in either trigger mode.
    if (active?.anchor === hint) clearTimeout(active.leaveTimer);
    if (getTooltipTrigger() === 'hover') showHint(hint, modal);
  });
  modal.addEventListener('pointerout', event => {
    const hint = hintFor(event.target);
    if (hint && event.pointerType !== 'touch' && !hint.contains(event.relatedTarget)) scheduleLeave(hint);
  });
  modal.addEventListener('focusin', event => {
    if (getTooltipTrigger() !== 'hover') return;
    const hint = hintFor(event.target);
    if (hint) showHint(hint, modal).keepOnFocus = true;
  });
  modal.addEventListener('focusout', event => {
    if (active?.anchor === hintFor(event.target) && !active.popup.contains(event.relatedTarget)) dismissSettingsHelp();
  });
  modal.addEventListener('click', event => {
    const hint = hintFor(event.target);
    if (!hint) return;
    event.preventDefault();
    event.stopPropagation();
    if (active?.anchor === hint && active.activated) dismissSettingsHelp();
    else {
      const state = showHint(hint, modal);
      state.activated = true;
      state.keepOnFocus = event.detail === 0;
    }
  });
}
