// No src exists until Play, so merely opening a job page cannot fetch media.
export function createVideoJobMedia(url, { type = 'video', label = 'Play video' } = {}) {
  const node = document.createElement('div'); node.className = 'video-job-media';
  const media = document.createElement(type); media.controls = true; media.preload = 'none'; media.hidden = true;
  if (type === 'video') media.playsInline = true;
  const play = document.createElement('button'); play.type = 'button'; play.className = 'memory-toolbar-btn'; play.textContent = label;
  const error = document.createElement('p'); error.className = 'h3-video-error'; error.hidden = true; error.setAttribute('role', 'status');
  play.onclick = async () => {
    error.hidden = true; media.hidden = false; play.hidden = true;
    if (!media.getAttribute('src')) media.src = url;
    try { await media.play(); }
    catch (reason) {
      if (!node.isConnected || !media.getAttribute('src')) return;
      play.hidden = false;
      if (reason.name !== 'AbortError') { error.textContent = 'Playback could not start. Try Play again or download this file.'; error.hidden = false; }
    }
  };
  media.addEventListener('error', () => {
    if (!media.getAttribute('src')) return;
    play.hidden = false; error.textContent = 'This browser could not play this file. Download it to use another player.'; error.hidden = false;
  });
  node.append(play, media, error); return { node, media };
}

export function releaseVideoJobMedia(container) {
  for (const media of container.querySelectorAll('video, audio')) {
    media.pause(); media.removeAttribute('src');
    for (const source of media.querySelectorAll('source')) source.remove();
    media.load();
  }
}
