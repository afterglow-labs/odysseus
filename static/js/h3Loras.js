// Ordered H3 adapter selections shared by the draft and bulk parameter editor.
export function h3LoraStack(config = {}) {
  if (Object.prototype.hasOwnProperty.call(config, 'loras')) {
    return Array.isArray(config.loras) ? config.loras.map(item => ({ id: String(item.id || ''), strength: item.strength ?? 1 })) : [];
  }
  return config.lora ? [{ id: String(config.lora), strength: config.lora_scale ?? 1 }] : [];
}

export function h3LoraIssue(stack, components = [], mode = 'ref2va', max = 8) {
  if (stack.length > max) return `Choose at most ${max} LoRAs.`;
  const ids = new Set(), variant = mode === 'ref2va' ? 'ref2va' : 'fl2va';
  for (const { id, strength } of stack) {
    if (ids.has(id)) return 'Each LoRA can be selected only once. Remove the duplicate selection.';
    ids.add(id);
    const item = components.find(component => component.role === 'lora' && component.id === id);
    if (!item) return 'A selected LoRA is unavailable. Mount its drive and refresh components, or remove it.';
    if (String(strength).trim() === '' || !Number.isFinite(Number(strength)) || Number(strength) < -4 || Number(strength) > 4) return 'Each LoRA strength must be between −4 and 4.';
    if (Number(strength) !== 0 && item.variant && item.variant !== 'shared' && item.variant !== variant) return `${item.name} requires ${item.variant.toUpperCase()} mode. Choose a compatible LoRA, set its strength to 0, or remove it.`;
  }
  return '';
}

export function createH3LoraEditor({ components = [], value = [], max = 8, onChange = () => {}, idPrefix = 'h3' } = {}) {
  const node = document.createElement('section'); node.className = 'h3-lora-editor';
  node.setAttribute('aria-label', 'LoRA stack');
  const list = document.createElement('div'); list.className = 'h3-lora-list';
  const add = document.createElement('button'); add.type = 'button'; add.className = 'memory-toolbar-btn'; add.textContent = 'Add LoRA';
  const help = document.createElement('p'); help.className = 'h3-video-muted';
  help.textContent = 'Applied in the order shown. Each LoRA has its own strength; 0 turns it off. Combining LoRAs may require different sampling settings.';
  node.append(list, add, help);
  let rows = [], disabled = false;
  const getValue = () => rows.filter(row => row.select.value).map(row => ({ id: row.select.value, strength: row.strength.value.trim() === '' ? '' : Number(row.strength.value) }));
  function refreshDisabled() {
    rows.forEach(row => { row.select.disabled = row.remove.disabled = disabled; row.strength.disabled = disabled || !row.select.value; });
    add.disabled = disabled || rows.length >= max || !components.some(item => item.role === 'lora');
  }
  function options(row, selected = row.select.value) {
    row.select.replaceChildren(); row.select.add(new Option('Choose LoRA…', ''));
    for (const item of components.filter(item => item.role === 'lora')) row.select.add(new Option(item.name, item.id));
    if (selected && !components.some(item => item.role === 'lora' && item.id === selected)) row.select.add(new Option(`Unavailable: ${selected.startsWith('unavailable:') ? selected.split(':').slice(2).join(':') : selected}`, selected));
    row.select.value = selected;
  }
  function renumber() {
    rows.forEach((row, index) => {
      row.label.textContent = `LoRA ${index + 1}`;
      row.select.id = `${idPrefix}-lora${index ? `-${index + 1}` : ''}`;
      row.select.setAttribute('aria-label', `LoRA ${index + 1}`);
      row.strength.id = `${idPrefix}-lora_scale${index ? `-${index + 1}` : ''}`;
      row.strength.setAttribute('aria-label', `LoRA ${index + 1} strength`);
      row.remove.setAttribute('aria-label', `Remove LoRA ${index + 1}`);
    });
    refreshDisabled();
  }
  function append(item = { id: '', strength: 1 }) {
    const wrap = document.createElement('div'); wrap.className = 'h3-lora-row';
    const label = document.createElement('label'); label.className = 'h3-video-field';
    const text = document.createElement('span'), select = document.createElement('select'); select.className = 'cookbook-field-input';
    label.append(text, select);
    const strengthLabel = document.createElement('label'); strengthLabel.className = 'h3-video-field';
    const strengthText = document.createElement('span'); strengthText.textContent = 'Strength';
    const strength = document.createElement('input'); strength.type = 'number'; strength.className = 'cookbook-field-input'; strength.min = -4; strength.max = 4; strength.step = 0.05; strength.required = true; strength.value = item.strength;
    strengthLabel.append(strengthText, strength);
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'memory-toolbar-btn'; remove.textContent = 'Remove';
    wrap.append(label, strengthLabel, remove); list.appendChild(wrap);
    const row = { wrap, select, strength, remove, label: text }; rows.push(row); options(row, item.id); renumber();
    select.addEventListener('change', () => { refreshDisabled(); onChange(getValue()); });
    strength.addEventListener('input', () => onChange(getValue()));
    strength.addEventListener('change', () => onChange(getValue()));
    remove.onclick = () => { rows = rows.filter(value => value !== row); wrap.remove(); renumber(); onChange(getValue()); };
  }
  add.onclick = () => { if (!add.disabled) { append(); rows.at(-1).select.focus(); } };
  function setValue(value) { rows = []; list.replaceChildren(); value.forEach(append); refreshDisabled(); }
  setValue(value);
  return { node, getValue, setValue,
    setDisabled(value) { disabled = !!value; refreshDisabled(); },
    setInventory(value, limit = 8) { components = value; max = limit; rows.forEach(row => options(row)); refreshDisabled(); },
  };
}
