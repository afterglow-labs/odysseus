// Provider errors carry the route that failed, independent of later picks.
export function providerEndpointKey(value) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return '';
    const path = url.pathname.replace(/\/+$/, '').replace(/\/(?:chat\/completions|responses|completions|messages)$/, '');
    return url.origin + path;
  } catch { return ''; }
}

export function providerReconnectTarget(payload = {}, selected = {}) {
  if (payload.authentication_required !== true) return null;
  if (payload.status === 429 || payload.daybreak_supported === false
      || ['invalid_access_program', 'unsupported_access_program', 'access_program_not_enabled'].includes(payload.code)) return null;
  const endpointUrl = payload.endpoint_url || selected.endpoint_url || '';
  if (!providerEndpointKey(endpointUrl)) return null;
  return Object.freeze({
    provider: String(payload.provider || ''),
    endpoint_url: endpointUrl,
    // A pre-content fallback may use another account at the very same URL.
    // Only the server's actual route ID can identify that account safely.
    endpoint_id: String(payload.endpoint_id || ''),
  });
}

export function resolveReconnectEndpoint(target, endpoints) {
  const key = providerEndpointKey(target.endpoint_url);
  const matches = endpoints.filter(endpoint => providerEndpointKey(endpoint.base_url) === key);
  if (target.endpoint_id) {
    const exact = matches.find(endpoint => String(endpoint.id) === String(target.endpoint_id));
    if (!exact) throw new Error('This connection changed or was removed. Select it in Added Models before reconnecting.');
    return exact;
  }
  if (matches.length !== 1) throw new Error(matches.length
    ? 'More than one connection matches. Choose the account in Added Models before reconnecting.'
    : 'This connection is no longer configured. Add it again in Settings.');
  return matches[0];
}

export function appendProviderReconnectButton(container, target, options = {}) {
  if (!container || !target || container.querySelector('.provider-reconnect-btn')) return null;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'admin-btn-sm provider-reconnect-btn';
  button.textContent = 'Reconnect provider';
  button.style.marginTop = '8px';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const open = options.open || (async value => {
        const { openProviderReconnect } = await import('./admin.js');
        await openProviderReconnect(value);
      });
      await open(target);
    } catch (error) {
      const message = document.createElement('div');
      message.className = 'admin-error';
      message.textContent = error.message || 'Could not open provider settings.';
      container.appendChild(message);
    } finally { button.disabled = false; }
  });
  container.appendChild(button);
  return button;
}
