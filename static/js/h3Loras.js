// Ordered H3 adapter selections shared by the draft and bulk parameter editor.
export const H3_LORA_RECOMMENDED_FIELDS = ['steps', 'sampler', 'scheduler', 'shift_video', 'shift_audio', 'width', 'height', 'frames', 'reference_size', 'seed'];

// A saved job keeps the profile it used. Overlay it only in that editor,
// leaving the current library and the user's unsent draft unchanged.
export function h3LoraComponents(components = [], frozen = []) {
  if (!Array.isArray(frozen) || !frozen.length) return components;
  const replacements = new Map(frozen.filter(item => item?.role === 'lora' && item.id).map(item => [item.id, item]));
  const result = components.map(item => {
    const saved = replacements.get(item.id); if (!saved) return item;
    replacements.delete(item.id);
    const { display_name, strength_control, lora_profile, ...current } = item;
    return { ...current, ...saved };
  });
  return [...result, ...replacements.values()];
}

export function insertH3LoraTrigger(prompt, trigger, position = 'prefix', cursor = prompt.length) {
  trigger = String(trigger || '').trim();
  if (!trigger) return prompt;
  if (position === 'prefix') return prompt.trimStart().startsWith(trigger) ? prompt : `${trigger}${prompt ? '\n' + prompt : ''}`;
  if (position === 'suffix') return prompt.trimEnd().endsWith(trigger) ? prompt : `${prompt}${prompt ? '\n' : ''}${trigger}`;
  cursor = Math.max(0, Math.min(prompt.length, Number(cursor) || 0));
  return `${prompt.slice(0, cursor)}${trigger}${prompt.slice(cursor)}`;
}

export function h3LoraStrengthControl(component = {}) {
  component ||= {};
  const custom = component.strength_control;
  if (custom && custom.kind !== 'age' && [custom.min, custom.max, custom.step, custom.default].every(value => typeof value === 'number' && Number.isFinite(value))
      && custom.min < custom.max && custom.step > 0 && custom.default >= custom.min && custom.default <= custom.max) {
    return { ...custom, kind: 'custom', label: String(custom.label || 'Strength'),
      labels: { min: custom.labels?.min || '', zero: custom.labels?.zero || '', max: custom.labels?.max || '' } };
  }
  const ageName = 'mm-h3 - ref2va - age slider v2 - experimental.safetensors';
  if (component.strength_control?.kind === 'age' || String(component.name || '').split(/[\\/]/).at(-1).trim().toLowerCase() === ageName) {
    return { kind: 'age', label: 'Age', min: -5, max: 5, step: 0.1, default: 0,
      labels: { min: 'Youngest', zero: 'Neutral', max: 'Oldest' } };
  }
  return { kind: 'strength', label: 'Strength', min: -4, max: 4, step: 0.05, default: 1 };
}

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
    const item = components.find(component => component.role === 'lora' && (component.id === id || component.aliases?.includes(id)));
    if (!item) return 'A selected LoRA is unavailable. Mount its drive and refresh components, or remove it.';
    if (ids.has(item.id)) return 'Each LoRA can be selected only once. Remove the duplicate selection.';
    ids.add(item.id);
    const control = h3LoraStrengthControl(item);
    if (String(strength).trim() === '' || !Number.isFinite(Number(strength)) || Number(strength) < control.min || Number(strength) > control.max) {
      return control.kind === 'age' ? 'Age must be between −5 (youngest) and +5 (oldest). Set 0 for neutral.'
        : control.kind === 'custom' ? `${control.label} must be between ${control.min} and ${control.max}.` : 'Each LoRA strength must be between −4 and 4.';
    }
    if (Number(strength) !== 0 && item.variant && item.variant !== 'shared' && item.variant !== variant) return `${item.name} requires ${item.variant.toUpperCase()} mode. Choose a compatible LoRA, set its strength to 0, or remove it.`;
  }
  return '';
}

export function createH3LoraEditor({ components = [], value = [], max = 8, onChange = () => {}, idPrefix = 'h3', onManage, onInsertTrigger, onApplyRecommended } = {}) {
  const node = document.createElement('section'); node.className = 'h3-lora-editor';
  node.setAttribute('aria-label', 'LoRA stack');
  const list = document.createElement('div'); list.className = 'h3-lora-list';
  const add = document.createElement('button'); add.type = 'button'; add.className = 'memory-toolbar-btn'; add.textContent = 'Add LoRA';
  const manage = document.createElement('button'); manage.type = 'button'; manage.className = 'memory-toolbar-btn'; manage.textContent = 'Manage LoRAs'; manage.hidden = true;
  const actions = document.createElement('div'); actions.className = 'h3-video-actions'; actions.append(add, manage);
  const help = document.createElement('p'); help.className = 'h3-video-muted';
  help.textContent = 'Applied in the order shown. Each LoRA has its own strength; 0 turns it off. Combining LoRAs may require different sampling settings.';
  node.append(list, actions, help);
  let rows = [], disabled = false, libraryAvailable = false;
  const getValue = () => rows.filter(row => row.select.value).map(row => ({ id: row.select.value, strength: row.strength.value.trim() === '' ? '' : Number(row.strength.value) }));
  function refreshDisabled() {
    rows.forEach(row => {
      row.select.disabled = row.remove.disabled = disabled; row.strength.disabled = row.slider.disabled = disabled || !row.select.value;
      row.editProfile.disabled = row.insertTrigger.disabled = row.applyRecommended.disabled = disabled || !row.select.value;
    });
    add.disabled = disabled || rows.length >= max || !components.some(item => item.role === 'lora');
    manage.hidden = !onManage || !libraryAvailable; manage.disabled = disabled;
  }
  function options(row, selected = row.select.value) {
    selected = components.find(item => item.role === 'lora' && (item.id === selected || item.aliases?.includes(selected)))?.id || selected;
    row.select.replaceChildren(); row.select.add(new Option('Choose LoRA…', ''));
    for (const item of components.filter(item => item.role === 'lora')) row.select.add(new Option(item.display_name || item.lora_profile?.display_name || item.name, item.id));
    if (selected && !components.some(item => item.role === 'lora' && item.id === selected)) row.select.add(new Option(`Unavailable: ${selected.startsWith('unavailable:') ? selected.split(':').slice(2).join(':') : selected}`, selected));
    row.select.value = selected;
    updateStrengthControl(row);
    updateProfile(row);
  }
  function updateProfile(row) {
    const component = components.find(item => item.id === row.select.value), profile = component?.lora_profile;
    row.profilePanel.hidden = !component || !(profile || onManage && libraryAvailable);
    row.triggerText.textContent = profile?.trigger_words ? `Trigger (${profile.trigger_position || 'manual'}): ${profile.trigger_words}` : '';
    row.triggerText.hidden = !profile?.trigger_words;
    row.notes.textContent = profile?.notes || ''; row.notes.hidden = !row.notes.textContent;
    row.recommendedText.textContent = Object.keys(profile?.recommended_settings || {}).length
      ? 'Recommended: ' + Object.entries(profile.recommended_settings).map(([key, value]) => `${key.replaceAll('_', ' ')} ${value}`).join(' · ') : '';
    row.recommendedText.hidden = !row.recommendedText.textContent;
    row.editProfile.hidden = !onManage || !libraryAvailable;
    row.insertTrigger.hidden = !onInsertTrigger || !profile?.trigger_words;
    row.insertTrigger.textContent = profile?.trigger_position === 'manual' ? 'Insert trigger at cursor' : 'Insert trigger';
    row.applyRecommended.hidden = !onApplyRecommended || !Object.keys(profile?.recommended_settings || {}).length;
  }
  function updateStrengthControl(row, selectedByUser = false) {
    const item = components.find(value => value.id === row.select.value);
    const control = h3LoraStrengthControl(item), age = control.kind === 'age', sliderControl = control.kind !== 'strength';
    row.strengthText.textContent = control.label;
    row.strength.min = row.slider.min = control.min;
    row.strength.max = row.slider.max = control.max;
    row.strength.step = row.slider.step = control.step;
    row.ageControl.hidden = !sliderControl;
    if (selectedByUser && sliderControl) row.strength.value = control.default;
    row.slider.value = row.strength.value;
    const number = Number(row.strength.value);
    row.slider.setAttribute('aria-valuetext', `${number > 0 ? '+' : ''}${row.strength.value}${age ? number < 0 ? ' · younger' : number > 0 ? ' · older' : ' · neutral' : ''}`);
    row.strength.setAttribute('aria-label', `LoRA ${rows.indexOf(row) + 1} ${age ? 'age' : control.label.toLowerCase()}`);
    row.slider.setAttribute('aria-label', `LoRA ${rows.indexOf(row) + 1} ${age ? 'age' : control.label.toLowerCase()} slider`);
    const signed = value => value > 0 ? `+${value}` : String(value).replace('-', '−');
    row.marks.replaceChildren();
    for (const [value, label] of [[control.min, control.labels?.min], ...(control.min < 0 && control.max > 0 && Math.abs(control.min + control.max) < 0.000001 ? [[0, control.labels?.zero]] : []), [control.max, control.labels?.max]]) {
      const mark = document.createElement('span'); mark.textContent = `${signed(value)}${label ? ` · ${label}` : ''}`; row.marks.append(mark);
    }
    row.ageHelp.textContent = age ? 'Negative values make the subject younger; positive values make the subject older. 0 leaves this LoRA off.' : 'Use the range saved in this LoRA profile. A strength of 0 turns the LoRA off.';
  }
  function renumber() {
    rows.forEach((row, index) => {
      row.label.textContent = `LoRA ${index + 1}`;
      row.select.id = `${idPrefix}-lora${index ? `-${index + 1}` : ''}`;
      row.select.setAttribute('aria-label', `LoRA ${index + 1}`);
      row.strength.id = `${idPrefix}-lora_scale${index ? `-${index + 1}` : ''}`;
      row.slider.id = `${idPrefix}-lora_age${index ? `-${index + 1}` : ''}`;
      row.slider.setAttribute('aria-label', `LoRA ${index + 1} age slider`);
      updateStrengthControl(row);
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
    const ageControl = document.createElement('div'); ageControl.className = 'h3-lora-age-control'; ageControl.hidden = true;
    const slider = document.createElement('input'); slider.type = 'range'; slider.className = 'h3-lora-age-slider';
    const marks = document.createElement('div'); marks.className = 'h3-lora-age-marks';
    for (const value of ['−5 · Youngest', '0 · Neutral', '+5 · Oldest']) {
      const label = document.createElement('span'); label.textContent = value; marks.append(label);
    }
    const ageHelp = document.createElement('small'); ageHelp.className = 'h3-video-muted';
    ageHelp.textContent = 'Negative values make the subject younger; positive values make the subject older. 0 leaves this LoRA off.';
    ageControl.append(slider, marks, ageHelp);
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'memory-toolbar-btn'; remove.textContent = 'Remove';
    const profilePanel = document.createElement('div'); profilePanel.className = 'h3-lora-profile-summary';
    const triggerText = document.createElement('p'), notes = document.createElement('p'), recommendedText = document.createElement('p');
    for (const p of [triggerText, notes, recommendedText]) p.className = 'h3-video-muted';
    const profileActions = document.createElement('div'); profileActions.className = 'h3-video-actions';
    const editProfile = document.createElement('button'), insertTrigger = document.createElement('button'), applyRecommended = document.createElement('button');
    for (const [button, title] of [[editProfile, 'Edit profile'], [insertTrigger, 'Insert trigger'], [applyRecommended, 'Apply recommended settings']]) {
      button.type = 'button'; button.className = 'memory-toolbar-btn'; button.textContent = title; profileActions.append(button);
    }
    profilePanel.append(triggerText, notes, recommendedText, profileActions);
    wrap.append(label, strengthLabel, remove, ageControl, profilePanel); list.appendChild(wrap);
    const row = { wrap, select, strength, strengthText, ageControl, slider, marks, ageHelp, remove, label: text, profilePanel, triggerText, notes, recommendedText, editProfile, insertTrigger, applyRecommended };
    rows.push(row); options(row, item.id); renumber();
    select.addEventListener('change', () => { updateStrengthControl(row, true); updateProfile(row); refreshDisabled(); onChange(getValue()); });
    const strengthChanged = () => { updateStrengthControl(row); onChange(getValue()); };
    strength.addEventListener('input', strengthChanged);
    strength.addEventListener('change', strengthChanged);
    const slide = () => { strength.value = slider.value; updateStrengthControl(row); onChange(getValue()); };
    slider.addEventListener('input', slide); slider.addEventListener('change', slide);
    remove.onclick = () => { rows = rows.filter(value => value !== row); wrap.remove(); renumber(); onChange(getValue()); };
    editProfile.onclick = () => onManage?.(select.value, editProfile);
    insertTrigger.onclick = () => onInsertTrigger?.(components.find(item => item.id === select.value));
    applyRecommended.onclick = () => onApplyRecommended?.(components.find(item => item.id === select.value));
  }
  add.onclick = () => { if (!add.disabled) { append(); rows.at(-1).select.focus(); } };
  manage.onclick = () => onManage?.('', manage);
  function setValue(value) { rows = []; list.replaceChildren(); value.forEach(append); refreshDisabled(); }
  setValue(value);
  return { node, getValue, setValue,
    setLibraryAvailable(value) { libraryAvailable = !!value; rows.forEach(updateProfile); refreshDisabled(); },
    setDisabled(value) { disabled = !!value; refreshDisabled(); },
    setInventory(value, limit = 8) { components = value; max = limit; rows.forEach(row => options(row)); refreshDisabled(); },
  };
}
