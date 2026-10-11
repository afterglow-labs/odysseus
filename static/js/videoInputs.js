// Server videos stay on the server. Only their allowlisted, versioned
// descriptors travel with a submission; ordinary File objects still upload.
export function serverVideoInput(selection) {
  if (!selection || typeof selection.root !== 'string' || typeof selection.path !== 'string'
      || !Number.isSafeInteger(selection.size) || selection.size <= 0
      || typeof selection.mtime_ns !== 'string' || !/^\d+$/.test(selection.mtime_ns)) {
    throw new Error('The server returned an invalid video selection. Refresh the folder and try again.');
  }
  const serverFile = Object.freeze({ root: selection.root, path: selection.path,
    size: selection.size, mtime_ns: selection.mtime_ns });
  return Object.freeze({ name: selection.path.split('/').at(-1), size: selection.size, serverFile });
}

export function appendVideoInputs(body, uploads) {
  const serverInputs = {};
  for (const [field, files] of Object.entries(uploads || {})) {
    for (const [index, file] of (files || []).entries()) {
      if (file.serverFile) {
        (serverInputs[field] ||= []).push({ ...file.serverFile, index });
      } else {
        body.append(field, file, file.name);
      }
    }
  }
  if (Object.keys(serverInputs).length) body.append('server_inputs', JSON.stringify(serverInputs));
  return body;
}
