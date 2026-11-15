/* Meadow gallery UI */
'use strict';

const $ = (s) => document.querySelector(s);
const grid = $('#grid'), statusEl = $('#status'), moreBtn = $('#more'),
      sentinel = $('#sentinel'), emptyEl = $('#empty'),
      filterbar = $('#filterbar'), feedstats = $('#feedstats');

let rootHex = null, cursor = null, loading = false, kinds = 'media';
let profilePending = new Map();   // pubkey -> [card elements to update]

function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}
function fmtDate(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleDateString(undefined,
      { year: 'numeric', month: 'short', day: 'numeric' });
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
      (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function mediaUrl(it) {
  return '/media?server=' + encodeURIComponent(it.server) +
         '&sha256=' + encodeURIComponent(it.sha256);
}
function shortNpub(pk) {
  return pk.slice(0, 8) + '…' + pk.slice(-4);
}

// ------------------------------------------------------------------ browse
$('#browse-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const npub = $('#npub').value.trim();
  if (!npub) return;
  browse(npub, $('#force').checked);
});

function setStatus(html, spinning) {
  statusEl.hidden = !html;
  statusEl.innerHTML = html
    ? (spinning ? '<span class="spinner"></span>' : '') + html : '';
}

async function browse(npub, force) {
  grid.innerHTML = ''; cursor = null; rootHex = null;
  moreBtn.hidden = true; sentinel.hidden = true; emptyEl.hidden = true;
  filterbar.hidden = true;
  $('#browse-btn').disabled = true;
  setStatus('Looking up <b>' + esc(shortNpub(npub)) + '</b>…', true);
  try {
    const url = '/api/browse?npub=' + encodeURIComponent(npub) +
                (force ? '&force=1' : '');
    const resp = await fetch(url);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (line.startsWith('data: ')) {
          handleEvent(JSON.parse(line.slice(6)));
        }
      }
    }
  } catch (e) {
    setStatus('<span class="error">Connection failed: ' + esc(e) + '</span>');
  }
  $('#browse-btn').disabled = false;
}

function handleEvent(msg) {
  if (msg.type === 'progress') {
    switch (msg.stage) {
      case 'start':
        setStatus('Finding the follow list…', true); break;
      case 'follows-found':
        setStatus('Found <b>' + msg.count + '</b> follows; collecting '
          + 'media-server lists…', true); break;
      case 'servers':
        setStatus('Collecting media-server lists…', true); break;
      case 'media-events':
        setStatus('Scanning the graph\'s media posts… <b>'
          + (msg.blobs || 0) + '</b> so far', true); break;
      case 'collect':
        setStatus('Querying <b>' + msg.servers
          + '</b> media servers for blobs…', true); break;
      case 'blobs':
        setStatus('Querying media servers… <b>' + msg.blobs
          + '</b> blobs so far (' + msg.done + '/' + msg.total
          + ' servers)', true); break;
      case 'cache':
        setStatus('Using the cached graph (' + msg.age_hours
          + ' h old) — check "refresh" to re-walk.', true); break;
    }
  } else if (msg.type === 'done') {
    rootHex = msg.root_hex;
    setStatus('Done: <b>' + msg.follows + '</b> follows, <b>'
      + msg.servers + '</b> servers, <b>' + msg.stats.blobs
      + '</b> blobs found. Loading feed…');
    setTimeout(() => { if (statusEl.hidden === false && !loadingMore)
      setStatus(''); }, 2500);
    filterbar.hidden = false;
    loadFeed();
  } else if (msg.type === 'error') {
    setStatus('<span class="error">' + esc(msg.message) + '</span>');
  }
}

// -------------------------------------------------------------------- feed
async function loadFeed() {
  if (loading || !rootHex) return;
  loading = true;
  moreBtn.disabled = true;
  const before = cursor != null ? '&before=' + cursor : '';
  try {
    const resp = await fetch(
      `/api/feed?root=${rootHex}${before}&limit=30&kinds=${kinds}`);
    const data = await resp.json();
    if (!before) grid.innerHTML = '';
    render(data);
    cursor = data.items.length
      ? data.items[data.items.length - 1].created_at : cursor;
    sentinel.hidden = !data.has_more;
    moreBtn.hidden = !data.has_more;
    if (!grid.children.length && !data.has_more) {
      emptyEl.hidden = false;
      emptyEl.textContent = 'No media found for this social graph. '
        + 'Most users never publish a kind 10063 media-server list, and '
        + 'some servers require auth to list. Try "refresh", or another '
        + 'npub with active media posters.';
    } else { emptyEl.hidden = true; }
  } catch (e) {
    setStatus('<span class="error">Feed failed: ' + esc(e) + '</span>');
  }
  loading = false;
  loadingMore = false;
  moreBtn.disabled = false;
}
let loadingMore = false;

function render(data) {
  // update profile cards from a later batch
  for (const [pk, evs] of profilePending) {
    const p = data.profiles[pk];
    if (p && (p.name || p.picture)) {
      for (const el of evs) applyProfile(el, pk, p);
      profilePending.delete(pk);
    }
  }
  for (const it of data.items) addCard(it, data.profiles[it.pubkey]);
}

function applyProfile(el, pk, p) {
  const name = el.querySelector('.name');
  const av = el.querySelector('.avatar-slot');
  if (p.name && name) name.textContent = p.name;
  if (p.picture && av) {
    const img = document.createElement('img');
    img.className = 'avatar'; img.loading = 'lazy'; img.src = p.picture;
    img.onerror = () => img.remove();
    av.replaceChildren(img);
  }
}

function addCard(it, profile) {
  const card = document.createElement('div');
  card.className = 'card';
  const url = mediaUrl(it);
  const isImg = (it.type || '').startsWith('image/');
  const isVid = (it.type || '').startsWith('video/');
  const previewable = (isImg || isVid)
    && (it.size == null || it.size <= 26214400);

  let mediaHtml;
  if (previewable && isImg) {
    mediaHtml = `<img class="media" loading="lazy" src="${esc(url)}"
      alt="${esc(it.name || it.sha256.slice(0, 12))}">`;
  } else if (previewable && isVid) {
    mediaHtml = `<video class="media" controls preload="metadata"
      src="${esc(url)}"></video>`;
  } else {
    const icon = isVid ? '🎬' : isImg ? '🖼️' : '📎';
    mediaHtml = `<div class="file"><span class="icon">${icon}</span>
      <span>${esc(it.name || it.sha256.slice(0, 16) + '…')}</span>
      <span>${esc(it.type || 'unknown type')} · ${fmtSize(it.size)}</span></div>`;
  }

  const whoName = profile?.name || shortNpub(it.pubkey);
  const avatar = profile?.picture
    ? `<img class="avatar" loading="lazy" src="${esc(profile.picture)}">`
    : `<span class="avatar-ph">${esc(whoName[0]?.toUpperCase() || '?')}</span>`;

  card.innerHTML = `
    ${mediaHtml}
    <div class="meta">
      <span class="avatar-slot">${avatar}</span>
      <div class="who">
        <div class="name">${esc(whoName)}</div>
        <div class="sub">${esc(new URL(it.server).hostname)} ·
          ${fmtDate(it.created_at)} · ${fmtSize(it.size)}</div>
      </div>
      <a href="${esc(url)}" target="_blank" rel="noopener"
         title="Open original">↗</a>
    </div>`;
  if (!profile?.name || !profile?.picture) {
    const list = profilePending.get(it.pubkey) || [];
    list.push(card);
    profilePending.set(it.pubkey, list);
  }
  grid.appendChild(card);
}

moreBtn.addEventListener('click', loadFeed);
new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting && !loadingMore && cursor != null
      && !moreBtn.hidden) {
    loadingMore = true;
    loadFeed();
  }
}, { rootMargin: '600px' }).observe(sentinel);

document.querySelectorAll('.chip').forEach((chip) => {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach((c) =>
      c.classList.toggle('active', c === chip));
    kinds = chip.dataset.kinds;
    cursor = null;
    if (rootHex) loadFeed();
  });
});

// remember the last root
try {
  const last = localStorage.getItem('meadow-root');
  if (last) $('#npub').value = last;
} catch (e) {}
$('#browse-form').addEventListener('submit', () => {
  try { localStorage.setItem('meadow-root', $('#npub').value.trim()); }
  catch (e) {}
});
