import { bindMenuDismiss } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { serverVideoInput } from './videoInputs.js';

const node = (tag, className = '', text) => {
  const element = document.createElement(tag); element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};
const button = text => { const element = node('button', 'memory-toolbar-btn', text); element.type = 'button'; return element; };

export function serverImageURL(selection, family, detail = false) {
  if (!['h3', 'bfs'].includes(family)) throw new Error('Unknown workflow family.');
  const descriptor = serverVideoInput(selection).serverFile;
  return `/api/video/${family}/server-files/image?${new URLSearchParams({ ...descriptor, detail: String(detail) })}`;
}

export function createServerImageThumbnail(selection, family) {
  const frame = node('span', 'video-server-image-thumbnail');
  const image = node('img'); image.alt = ''; image.loading = 'lazy'; image.decoding = 'async';
  const status = node('small', 'h3-video-muted', 'Loading…');
  image.onload = () => { status.hidden = true; };
  image.onerror = () => { image.hidden = true; status.textContent = 'No preview'; frame.title = 'Image preview unavailable. You can still select this file.'; };
  image.src = serverImageURL(selection, family);
  frame.append(image, status);
  return frame;
}

export function createServerImageButton(selection, family) {
  const preview = button(''); preview.classList.add('video-server-image-button');
  const name = selection.path.split('/').at(-1);
  preview.title = `Preview ${name}`; preview.setAttribute('aria-label', preview.title);
  preview.append(createServerImageThumbnail(selection, family));
  preview.onclick = () => showServerImagePreview({ selection, family }, preview);
  return preview;
}

export function showServerImagePreview({ selection, family, isSelected, onToggleSelection }, anchor = document.activeElement) {
  const url = serverImageURL(selection, family, true), name = selection.path.split('/').at(-1);
  const overlay = node('div', 'cookbook-edit-overlay video-server-files-overlay video-server-image-overlay');
  overlay.style.zIndex = String(topPortalZ());
  const dialog = node('section', 'cookbook-edit-modal h3-video-dialog video-server-image-dialog');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', 'Image preview');
  const header = node('div', 'h3-video-header'), heading = node('div'), done = button('Done');
  heading.append(node('h2', '', 'Image preview'), node('p', 'h3-video-muted', name)); header.append(heading, done);
  const body = node('div', 'video-server-image-body'), image = node('img'); image.alt = name; image.hidden = true;
  const feedback = node('div', 'video-server-image-feedback'), status = node('p', 'h3-video-muted'), retry = button('Try again');
  status.setAttribute('role', 'status'); retry.hidden = true; feedback.append(status, retry); body.append(image, feedback);
  dialog.append(header, body);
  if (onToggleSelection) {
    const footer = node('div', 'video-server-files-footer'), select = button(isSelected?.() ? 'Deselect image' : 'Select image');
    select.onclick = () => { onToggleSelection(); close(); }; footer.append(select); dialog.append(footer);
  }
  overlay.append(dialog);
  const owner = anchor?.closest('[role="dialog"]'), wasInert = owner?.inert;
  if (owner) owner.inert = true;
  document.body.append(overlay);
  let controller, objectURL, closed = false;
  const close = bindMenuDismiss(overlay, () => {
    closed = true; controller?.abort(); image.removeAttribute('src');
    if (objectURL) URL.revokeObjectURL(objectURL);
    observer.disconnect(); window.removeEventListener('keydown', onKey, true); overlay.remove();
    if (owner) owner.inert = wasInert;
    if (anchor?.isConnected) anchor.focus();
    else if (owner?.isConnected) owner.querySelector('button:not(:disabled)')?.focus();
  }, event => event.target === overlay);
  const observer = new MutationObserver(() => { if (!overlay.isConnected || anchor && !anchor.isConnected) close(); });
  observer.observe(document.body, { childList: true, subtree: true });
  function onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(); }
    else if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled)')].filter(item => !item.hidden);
      if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); }
    }
  }
  async function load() {
    controller?.abort(); const current = new AbortController(); controller = current;
    image.removeAttribute('src'); image.hidden = true; feedback.hidden = false; retry.hidden = true;
    if (objectURL) URL.revokeObjectURL(objectURL); objectURL = null;
    status.textContent = 'Loading image preview…';
    try {
      const response = await fetch(url, { credentials: 'same-origin', signal: current.signal });
      if (!response.ok) {
        let message; try { message = (await response.json()).detail; } catch {}
        throw new Error(typeof message === 'string' ? message : `Image preview unavailable (${response.status}). You can still select this file.`);
      }
      const blob = await response.blob();
      if (closed || controller !== current) return;
      if (blob.type !== 'image/jpeg' || blob.size > 4 * 1024 * 1024) throw new Error('The server did not return an image preview.');
      objectURL = URL.createObjectURL(blob); image.src = objectURL;
      await image.decode();
      if (closed || controller !== current) return;
      image.hidden = false; feedback.hidden = true;
    } catch (error) {
      if (closed || controller !== current || error.name === 'AbortError') return;
      status.textContent = error.message || 'This image could not be previewed.'; retry.hidden = false;
    }
  }
  retry.onclick = load; done.onclick = close; window.addEventListener('keydown', onKey, true);
  done.focus(); load(); return { close };
}
