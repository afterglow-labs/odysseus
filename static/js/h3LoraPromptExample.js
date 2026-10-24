// Read-only guidance: viewing/copying an example never changes a render draft.
export function showLoraPromptExample(component, anchor) {
  const text = component?.lora_profile?.prompt_example;
  if (!text) return;
  const dialog = document.createElement('dialog'); dialog.className = 'h3-prompt-example-dialog';
  dialog.setAttribute('aria-label', 'LoRA example prompt');
  const header = document.createElement('header'), title = document.createElement('h2');
  title.textContent = 'Example prompt';
  const actions = document.createElement('div'); actions.className = 'h3-video-actions';
  const copy = document.createElement('button'), close = document.createElement('button');
  for (const [button, label] of [[copy, 'Copy prompt'], [close, 'Close']]) {
    button.type = 'button'; button.className = 'memory-toolbar-btn'; button.textContent = label; actions.append(button);
  }
  header.append(title, actions);
  const name = document.createElement('p'); name.className = 'h3-video-muted';
  name.textContent = component.display_name || component.lora_profile.display_name || component.name;
  const content = document.createElement('textarea'); content.readOnly = true; content.value = text;
  content.setAttribute('aria-label', 'Example prompt text'); content.spellcheck = false;
  const status = document.createElement('p'); status.className = 'h3-video-muted'; status.setAttribute('role', 'status');
  dialog.append(header, name, content, status);
  const dismiss = () => { dialog.close(); dialog.remove(); if (anchor?.isConnected) anchor.focus(); };
  close.onclick = dismiss;
  // Keep text selection, backdrop clicks and Escape from closing the workflow.
  dialog.addEventListener('cancel', event => event.preventDefault());
  const onKey = event => { if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); } };
  window.addEventListener('keydown', onKey, true);
  dialog.addEventListener('close', () => window.removeEventListener('keydown', onKey, true), { once: true });
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(text); status.textContent = 'Prompt copied.';
    } catch {
      content.focus(); content.select();
      status.textContent = document.execCommand('copy') ? 'Prompt copied.' : 'Text selected. Use Copy on your device.';
    }
  };
  document.body.append(dialog); dialog.showModal(); close.focus();
  return { close: dismiss };
}
