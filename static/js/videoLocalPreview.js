// One temporary, owner-scoped browser preview per local File. The original File
// remains the workflow attachment; neither this handle nor its MP4 replaces it.
const API = '/api/video/h3/local-previews';
const entries = new WeakMap();
const validId = id => typeof id === 'string' && /^[a-f0-9]{32}$/.test(id);
const remove = id => { if (validId(id)) fetch(`${API}/${id}`, { method: 'DELETE', credentials: 'same-origin', keepalive: true }).catch(() => {}); };

export function acquireLocalVideoPreview(file) {
  let entry = entries.get(file);
  if (!entry) { entry = { clients: new Map(), data: null, pending: null, xhr: null, cleanup: null, retired: false, progress: null }; entries.set(file, entry); }
  clearTimeout(entry.cleanup);
  const owner = Symbol(); entry.clients.set(owner, null);
  let released = false;
  const report = progress => { entry.progress = progress; for (const listener of entry.clients.values()) listener?.(progress); };
  return {
    load(onProgress = () => {}) {
      if (released) return Promise.reject(new DOMException('Preview closed.', 'AbortError'));
      entry.clients.set(owner, onProgress);
      if (entry.data && entry.data.expires_at * 1000 <= Date.now()) { remove(entry.data.id); entry.data = null; }
      if (entry.data) return Promise.resolve(entry.data);
      if (entry.pending) { if (entry.progress) onProgress(entry.progress); return entry.pending; }
      entry.pending = new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest(); entry.xhr = xhr;
        xhr.open('POST', API); xhr.withCredentials = true;
        xhr.upload.onprogress = event => report({ phase: 'uploading', loaded: event.loaded, total: event.lengthComputable ? event.total : file.size });
        xhr.upload.onload = () => report({ phase: 'preparing' });
        xhr.onerror = () => reject(new Error('Preview upload lost its connection. Retry compatible preview.'));
        xhr.onabort = () => reject(new DOMException('Preview upload cancelled.', 'AbortError'));
        xhr.onload = () => {
          let data; try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
          if (xhr.status < 200 || xhr.status >= 300) { reject(new Error(typeof data.detail === 'string' ? data.detail : `Compatible preview could not be prepared (${xhr.status}).`)); return; }
          if (!validId(data.id) || data.preview_url !== `${API}/${data.id}/video`
              || !Number.isFinite(data.metadata?.duration_seconds) || data.metadata.duration_seconds <= 0
              || !Number.isFinite(data.expires_at)) {
            remove(data.id); reject(new Error('The server did not return a valid compatible preview. Retry.')); return;
          }
          if (entry.retired || !entry.clients.size) { remove(data.id); reject(new DOMException('Preview closed.', 'AbortError')); return; }
          entry.data = data; resolve(data);
        };
        report({ phase: 'uploading', loaded: 0, total: file.size });
        const body = new FormData(); body.append('video', file, file.name); xhr.send(body);
      }).finally(() => { entry.pending = null; entry.xhr = null; entry.progress = null; });
      return entry.pending;
    },
    cancel() { entry.xhr?.abort(); },
    invalidate(id) { if (entry.data?.id === id) { remove(id); entry.data = null; } },
    release() {
      if (released) return; released = true; entry.clients.delete(owner);
      if (entry.clients.size) return;
      // Existing upload lists rebuild their rows when another attachment is
      // added. Let the new row reclaim the same File before discarding it.
      entry.cleanup = setTimeout(() => {
        if (entry.clients.size) return;
        entry.retired = true; entries.delete(file); entry.xhr?.abort();
        if (entry.data) remove(entry.data.id);
      }, 250);
    },
  };
}
