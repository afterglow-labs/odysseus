// Read-only guidance: viewing/copying an example never changes a render draft.
export function showLoraPromptExample(component, anchor) {
  const text = component?.lora_profile?.prompt_example;
  if (!text) return;
  const { dialog, header, actions, close, dismiss } = loraReadOnlyDialog('Example prompt', 'LoRA example prompt', anchor);
  const copy = document.createElement('button');
  copy.type = 'button'; copy.className = 'memory-toolbar-btn'; copy.textContent = 'Copy prompt'; actions.prepend(copy);
  const name = document.createElement('p'); name.className = 'h3-video-muted';
  name.textContent = component.display_name || component.lora_profile.display_name || component.name;
  const content = document.createElement('textarea'); content.readOnly = true; content.value = text;
  content.setAttribute('aria-label', 'Example prompt text'); content.spellcheck = false;
  const status = document.createElement('p'); status.className = 'h3-video-muted'; status.setAttribute('role', 'status');
  dialog.append(header, name, content, status);
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

function loraReadOnlyDialog(heading, accessibleLabel, anchor) {
  const dialog = document.createElement('dialog'); dialog.className = 'h3-prompt-example-dialog';
  dialog.setAttribute('aria-label', accessibleLabel);
  const header = document.createElement('header'), title = document.createElement('h2');
  title.textContent = heading;
  const actions = document.createElement('div'); actions.className = 'h3-video-actions';
  const close = document.createElement('button');
  close.type = 'button'; close.className = 'memory-toolbar-btn'; close.textContent = 'Close'; actions.append(close);
  header.append(title, actions);
  const dismiss = () => { dialog.close(); dialog.remove(); if (anchor?.isConnected) anchor.focus(); };
  close.onclick = dismiss;
  // Keep text selection, backdrop clicks and Escape from closing the workflow.
  dialog.addEventListener('cancel', event => event.preventDefault());
  const onKey = event => { if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); } };
  window.addEventListener('keydown', onKey, true);
  dialog.addEventListener('close', () => window.removeEventListener('keydown', onKey, true), { once: true });
  return { dialog, header, actions, close, dismiss };
}

export function loraHelpSourceURL(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function showLoraHelp(component, anchor) {
  if (!component) return;
  const help = component.lora_help || {
    title: component.display_name || component.name,
    summary: 'An installed LoRA adapter. Publisher documentation has not been identified for this file yet.',
    trigger: 'Unknown. An empty trigger field does not mean no trigger is needed. Check the publisher’s README or your saved profile.',
  };
  const { dialog, header, close, dismiss } = loraReadOnlyDialog('LoRA help', 'LoRA help', anchor);
  const scroll = document.createElement('div'); scroll.className = 'h3-video-scroll h3-lora-help-content';
  function section(title, paragraphs = []) {
    if (!paragraphs.length) return null;
    const node = document.createElement('section'), heading = document.createElement('h3'); heading.textContent = title; node.append(heading);
    for (const text of paragraphs) { const p = document.createElement('p'); p.textContent = text; node.append(p); }
    scroll.append(node); return node;
  }
  section('What it does', [help.title || component.name, help.summary, component.name].filter(Boolean));
  const trigger = section('How to trigger it', [help.trigger || 'No trigger information has been documented.']);
  if (help.trigger_words) {
    const words = document.createElement('textarea'); words.readOnly = true; words.value = help.trigger_words; words.rows = 2;
    words.setAttribute('aria-label', 'LoRA trigger text');
    const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'memory-toolbar-btn'; copy.textContent = 'Copy trigger';
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(help.trigger_words); status.textContent = 'Trigger copied.'; }
      catch { words.focus(); words.select(); status.textContent = document.execCommand('copy') ? 'Trigger copied.' : 'Text selected. Use Copy on your device.'; }
    };
    trigger.append(words, copy, status);
  }
  section('How to use it', help.usage); section('Settings', help.settings); section('Documentation notes', help.caveats);
  const links = section('README and sources', [help.sources?.length ? 'Publisher documentation:' : 'No verified publisher link is available for this file yet.']);
  for (const source of help.sources || []) {
    const url = loraHelpSourceURL(source.url); if (!url) continue;
    const link = document.createElement('a'); link.href = url; link.textContent = source.label || 'Source';
    link.target = '_blank'; link.rel = 'noopener noreferrer'; const p = document.createElement('p'); p.append(link); links.append(p);
  }
  if (help.verified_on) { const p = document.createElement('p'); p.className = 'h3-video-muted'; p.textContent = 'Sources checked ' + help.verified_on; links.append(p); }
  const profile = component.lora_profile || {};
  section('Your saved profile', [profile.trigger_words ? 'Saved trigger: ' + profile.trigger_words : '', profile.notes].filter(Boolean));
  dialog.append(header, scroll);
  document.body.append(dialog); dialog.showModal(); close.focus();
  return { close: dismiss };
}
