// Presentation follows declared metadata without changing execution values.
export function graphFieldMultiline(field, spec = {}) {
  const explicit = field.multiline ?? field.metadata?.multiline;
  if (typeof explicit === 'boolean') return explicit;
  if (field.editor === 'textarea') return true;
  return spec.execution !== 'comfy' && (field.type === 'text' || ['text','prompt','negative_prompt'].includes(field.id));
}

export function graphFieldUsesPromptHistory(document, node, field) {
  const textFields = new Set(['text', 'text_g', 'text_l', 'prompt', 'positive', 'positive_prompt']);
  if (!textFields.has(field.id)) return false;
  const nodes = new Map((document.nodes || []).map(item => [item.id, item]));
  const edges = document.edges || [];
  function upstream(ports, positive) {
    const found = new Set(), pending = edges.filter(edge => ports.includes(edge.target.port)).map(edge => edge.source.node);
    while (pending.length) {
      const id = pending.pop(); if (found.has(id)) continue; found.add(id);
      const reroute = /reroute/i.test(nodes.get(id)?.type || '');
      for (const edge of edges) if (edge.target.node === id && (!positive || reroute || textFields.has(edge.target.port))) pending.push(edge.source.node);
    }
    return found;
  }
  const positive = upstream(['positive', 'positive_prompt'], true);
  const negative = upstream(['negative', 'negative_prompt'], false);
  if (negative.has(node.id) && !positive.has(node.id)) return false;
  return positive.has(node.id) || ['prompt', 'positive_prompt', 'positive'].includes(field.id)
    || (node.type === 'input.prompt' && field.id === 'text');
}

export function graphOutputKind(output = {}) {
  const name = String(output.name || output.filename || '').toLowerCase();
  const mime = String(output.mime_type || output.content_type || '').toLowerCase();
  if (/\.zip$/.test(name) || ['application/zip','application/x-zip-compressed'].includes(mime)) return 'archive';
  if (mime.startsWith('video/') || /\.(mp4|webm|mov|m4v|mkv)$/.test(name)) return 'video';
  if (mime.startsWith('audio/') || /\.(wav|mp3|m4a|ogg|flac|aac)$/.test(name)) return 'audio';
  if (mime.startsWith('image/') || /\.(png|jpe?g|webp|gif|avif)$/.test(name)) return 'image';
  return 'download';
}

export function graphPortHelp(port, hasWidget = false) {
  const notes = [];
  if (hasWidget) notes.push('A connection overrides the setting above. Disconnect to use that setting again.');
  else notes.push(port.required ? 'An upstream connection is required.' : 'This input is optional.');
  if (port.force_input) notes.push('This package declares a connection-only input.');
  if (port.is_list) notes.push('Receives a list through one connection; the execution core handles list values.');
  if (port.multiple) notes.push('Accepts multiple upstream connections.');
  if (port.metadata?.tooltip) notes.push(port.metadata.tooltip);
  return notes.join(' ');
}
