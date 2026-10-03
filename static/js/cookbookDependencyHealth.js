import { bindMenuDismiss, dismissOrRemove, dismissTopMenu } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';

// A missing primary package keeps its normal Install button. Index failures
// add explanatory findings without turning an unknown result into a repair.
export function dependencyIssues(packages) {
  return (packages || []).flatMap(pkg => {
    const issues = pkg.dependency_issues?.length ? [...pkg.dependency_issues]
      : (pkg.needs_repair ? [{ name: pkg.name, kind: 'incompatible', message: pkg.status_note || 'Package needs repair.' }] : []);
    if (pkg.compatibility_note && (pkg.install_supported === false || pkg.has_compatible_artifact !== true)) {
      issues.push({ name: pkg.name, kind: 'compatibility', message: pkg.compatibility_note, metadata_only: true });
    }
    return issues.map(issue => ({ pkg, issue }));
  });
}

export function showDependencyIssues(packages, onInstall, anchor = document.activeElement) {
  document.querySelectorAll('.cookbook-dependency-health').forEach(dismissOrRemove);
  const entries = dependencyIssues(packages);
  if (!entries.length) return null;
  const element = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const overlay = element('div', 'cookbook-edit-overlay cookbook-dependency-health');
  overlay.style.zIndex = String(topPortalZ());
  const dialog = element('div', 'cookbook-edit-modal');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-label', 'Dependency check');
  dialog.style.width = 'min(560px, calc(100vw - 32px))';
  dialog.appendChild(element('div', 'cookbook-edit-title', 'Dependency check'));
  const list = element('div');
  list.style.cssText = 'max-height:60vh;overflow-y:auto;display:flex;flex-direction:column;gap:12px;';
  for (const { pkg, issue } of entries) {
    const row = element('div');
    row.style.cssText = 'display:flex;gap:12px;align-items:center;';
    const details = element('div');
    details.style.cssText = 'flex:1;min-width:0;overflow-wrap:anywhere;';
    details.appendChild(element('div', 'memory-item-title', issue.name || issue.requirement || 'Dependency'));
    details.appendChild(element('div', 'memory-item-meta', issue.metadata_only
      ? 'Release compatibility'
      : `${issue.kind === 'missing' ? 'Missing' : 'Needs repair'} · Required by ${pkg.name}`));
    if (issue.metadata_only && pkg.latest_version) details.appendChild(element('div', 'memory-item-meta', `Latest release: ${pkg.latest_version}${pkg.requires_python ? ` · Requires Python ${pkg.requires_python}` : ''}`));
    const versionNote = [issue.installed_version ? `Installed: ${issue.installed_version}` : '', issue.requirement ? `Requires: ${issue.requirement}` : ''].filter(Boolean).join(' · ');
    if (versionNote) details.appendChild(element('div', 'memory-item-meta', versionNote));
    if (issue.message) details.appendChild(element('div', 'memory-item-meta', issue.message));
    if (pkg.install_supported === false && pkg.install_hint) details.appendChild(element('div', 'memory-item-meta', pkg.install_hint));
    row.appendChild(details);
    if (!issue.metadata_only && (issue.requirement || pkg.pip) && pkg.install_supported !== false) {
      const label = issue.requirement && issue.kind === 'missing' ? 'Install' : 'Repair';
      const button = element('button', 'cookbook-dep-tag cookbook-dep-install', label);
      button.type = 'button';
      button.setAttribute('aria-label', `${label} ${issue.name || issue.requirement}`);
      button.addEventListener('click', async () => {
        if (button.disabled) return;
        button.disabled = true;
        button.textContent = 'Starting…';
        try {
          const started = await onInstall(pkg, issue, button);
          button.textContent = started ? 'Installing…' : label;
          button.disabled = !!started;
        } catch {
          button.textContent = label;
          button.disabled = false;
        }
      });
      row.appendChild(button);
    }
    list.appendChild(row);
  }
  dialog.appendChild(list);
  const actions = element('div', 'cookbook-edit-actions');
  const done = element('button', 'memory-toolbar-btn', 'Close');
  done.type = 'button';
  actions.appendChild(done);
  dialog.appendChild(actions);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  const onKey = event => {
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation();
      dismissTopMenu();
    } else if (event.key === 'Tab') {
      const buttons = [...dialog.querySelectorAll('button:not(:disabled)')];
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  };
  const close = bindMenuDismiss(overlay, () => {
    window.removeEventListener('keydown', onKey, true);
    overlay.remove();
    if (anchor?.isConnected) anchor.focus();
  }, event => event.target === overlay);
  done.addEventListener('click', close);
  window.addEventListener('keydown', onKey, true);
  done.focus();
  return overlay;
}
