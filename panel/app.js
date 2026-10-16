const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const enc = encodeURIComponent;
const join = (a, b) => [a, b].filter(Boolean).join('/');
const cleanFolder = s => s.trim().replace(/^\/+|\/+$/g, '');

// ---------- theme (system | light | dark), remembered
let theme = 'system';
try { theme = localStorage.getItem('theme') || 'system'; } catch (e) {}
function applyTheme() {
  const r = document.documentElement;
  if (theme === 'light' || theme === 'dark') r.dataset.theme = theme; else delete r.dataset.theme;
  const b = $('#themeToggle');
  if (b) b.textContent = theme === 'light' ? '☀️' : theme === 'dark' ? '🌙' : '🌓';
}
function cycleTheme() {
  theme = theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system';
  try { localStorage.setItem('theme', theme); } catch (e) {}
  applyTheme();
}
applyTheme();
if ($('#themeToggle')) $('#themeToggle').onclick = cycleTheme;

// ---------- PWA service worker (offline app shell, installable)
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}

function fmtSize(n) {
  n = Number(n) || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 2 : 1) : n) + ' ' + u[i];
}
function fmtEta(s) {
  if (!isFinite(s) || s <= 0) return '-';
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = Math.floor(s % 60);
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}
function fmtDate(t) {
  const d = typeof t === 'number' ? new Date(t * 1000) : new Date(t);
  return isNaN(d) ? '' : d.toLocaleString();
}
function toast(msg, err) {
  const el = document.createElement('div');
  el.textContent = msg;
  if (err) el.className = 'err';
  $('#toast').appendChild(el);
  setTimeout(() => el.remove(), err ? 7000 : 3500);
}

async function api(path, opts = {}) {
  const headers = { 'X-Panel': '1' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(path, { method: opts.method || 'GET', headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  if (r.status === 401) { showLogin(); throw new Error('লগইন দরকার'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
const post = (p, b) => api(p, { method: 'POST', body: b || {} });
const aria = (method, params = []) => post('/api/aria2', { method, params }).then(d => d.result);
async function run(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { if (e.message !== 'লগইন দরকার') toast(e.message, true); }
}

// navigator.clipboard only exists on HTTPS; plain-HTTP installs need the textarea fallback.
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) {}
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  ta.remove();
  return ok;
}

// ---------- session / roles
let info = { role: 'viewer', remotes: [], state: {}, tools: {} };
const RANK = { viewer: 1, user: 2, admin: 3 };
const can = role => RANK[info.role] >= RANK[role];
function applyRoles() {
  $$('[data-role]').forEach(el => { el.style.display = can(el.dataset.role) ? '' : 'none'; });
}
function showLogin() { $('#app').classList.add('hidden'); $('#login').classList.remove('hidden'); $('#loginPass').focus(); }
function showApp() { $('#login').classList.add('hidden'); $('#app').classList.remove('hidden'); }
$('#loginForm').onsubmit = async e => {
  e.preventDefault();
  try {
    await post('/api/login', { username: $('#loginUser').value.trim() || 'admin', password: $('#loginPass').value });
    $('#loginPass').value = '';
    start();
  } catch (err) { toast(err.message, true); }
};
$('#logout').onclick = async () => { await post('/api/logout'); location.reload(); };

// ---------- tabs
let tab = 'downloads';
function setTab(name) {
  const btn = $(`#nav [data-tab="${name}"]`);
  if (!btn || btn.style.display === 'none') name = 'downloads';
  tab = name;
  $$('#nav button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('main > section').forEach(s => s.classList.toggle('hidden', s.id !== 'tab-' + name));
  try { localStorage.setItem('tab', name); } catch (e) {}
  if (name === 'files') loadFiles();
  if (name === 'cloud') loadCloud();
  if (name === 'rss') loadRss();
  if (name === 'settings') loadSettings();
  tick();
}
$('#nav').onclick = e => { const b = e.target.closest('button'); if (b) setTab(b.dataset.tab); };

// ---------- info / banners
function fillRemoteSelects() {
  $$('.remoteSelect').forEach(sel => {
    const cur = sel.value;
    sel.innerHTML = info.remotes.map(r => `<option value="${esc(r.name)}">${esc(r.label)} (${esc(r.name)})</option>`).join('')
      || '<option value="">কোনো Cloud একাউন্ট নেই</option>';
    if (info.remotes.some(r => r.name === cur)) sel.value = cur;
  });
}
async function loadInfo() {
  info = await api('/api/info');
  $('#stDisk').textContent = fmtSize(info.disk_free);
  $('#stUser').textContent = info.user;
  fillRemoteSelects();
  applyRoles();
  const b = [];
  if (info.disk_low) b.push(['bad', `⚠️ ডিস্কে জায়গা কম (${fmtSize(info.disk_free)} খালি)। ডাউনলোড থামানো হয়েছে। কিছু ফাইল মুছে ▶ চালু করুন।`]);
  if (info.schedule_paused) b.push(['', '⏰ এখন ডাউনলোডের সময় না (সেটিং-এ সময় ঠিক করা আছে), তাই ডাউনলোড থেমে আছে।']);
  $('#banners').innerHTML = b.map(([c, t]) => `<div class="banner ${c}">${esc(t)}</div>`).join('');
}

// ---------- downloads
function updateDlForm() {
  const t = $('#dlTarget').value;
  $('#dlRemoteBox').classList.toggle('hidden', t !== 'cloud');
  $('#dlQualityBox').classList.toggle('hidden', t === 'cloud');
  $('#dlPlaylistBox').classList.toggle('hidden', t === 'cloud');
  $('#dlSubsBox').classList.toggle('hidden', t === 'cloud');
  $('#dlFolderLabel').textContent = t === 'cloud' ? 'Cloud-এর ফোল্ডার (খালি = মূল ফোল্ডার)' : 'সার্ভারের ফোল্ডার (খালি = মূল ফোল্ডার)';
}
$('#dlTarget').onchange = updateDlForm;

$('#dlAdd').onclick = () => run(async () => {
  const urls = $('#dlLinks').value.split('\n').map(s => s.trim()).filter(Boolean);
  if (!urls.length) throw new Error('আগে লিংক দিন');
  const r = await post('/api/add', { urls, target: $('#dlTarget').value, folder: cleanFolder($('#dlFolder').value),
    quality: $('#dlQuality').value, playlist: $('#dlPlaylist').checked, remote: $('#dlRemote').value,
    subs: $('#dlSubs').checked ? $('#dlSubsLang').value.trim() : '' });
  const parts = [];
  if (r.aria2) parts.push(`⬇ ${r.aria2}টা ডাউনলোড শুরু`);
  if (r.video) parts.push(`🎬 ${r.video}টা ভিডিও (ট্রান্সফার ট্যাবে দেখুন)`);
  if (r.cloud) parts.push(`☁ ${r.cloud}টা Cloud কাজ (ট্রান্সফার ট্যাবে দেখুন)`);
  if (parts.length) { toast(parts.join('\n')); $('#dlLinks').value = r.errors.length ? r.errors.map(e => e.split(': ')[0]).join('\n') : ''; }
  r.errors.forEach(e => toast(e, true));
  tick();
});

let ariaDir = null;
async function serverDir(folder) {
  if (!ariaDir) ariaDir = (await aria('aria2.getGlobalOption')).dir;
  return ariaDir.replace(/\/+$/, '') + '/' + folder;
}
$('#dlTorrent').onchange = e => run(async () => {
  const folder = cleanFolder($('#dlFolder').value);
  for (const f of e.target.files) {
    const b64 = await new Promise((ok, bad) => {
      const r = new FileReader();
      r.onload = () => ok(r.result.split(',')[1]);
      r.onerror = bad;
      r.readAsDataURL(f);
    });
    await aria('aria2.addTorrent', [b64, [], folder ? { dir: await serverDir(folder) } : {}]);
  }
  e.target.value = '';
  tick();
}, 'Torrent যোগ হয়েছে');

$('#dlPauseAll').onclick = () => run(() => aria('aria2.pauseAll'), 'সব থামানো হয়েছে');
$('#dlResumeAll').onclick = () => run(() => aria('aria2.unpauseAll'), 'সব চালু হয়েছে');
$('#dlPurge').onclick = () => run(() => aria('aria2.purgeDownloadResult'), 'শেষগুলো তালিকা থেকে মুছেছে').then(tick);

function dlName(d) {
  if (d.bittorrent && d.bittorrent.info && d.bittorrent.info.name) return d.bittorrent.info.name;
  const f = d.files && d.files[0];
  if (f && f.path) return f.path.split('/').pop();
  if (f && f.uris && f.uris[0]) return f.uris[0].uri;
  return d.gid;
}
const STATUS = { active: ['চলছে', ''], waiting: ['অপেক্ষায়', 'warn'], paused: ['থামানো', 'warn'],
  complete: ['শেষ ✔', 'ok'], error: ['সমস্যা', 'bad'], removed: ['বাতিল', 'bad'] };

async function loadDownloads() {
  const keys = ['gid', 'status', 'totalLength', 'completedLength', 'downloadSpeed', 'uploadSpeed',
    'connections', 'numSeeders', 'errorMessage', 'files', 'bittorrent', 'dir', 'followedBy'];
  const res = await aria('system.multicall', [[
    { methodName: 'aria2.tellActive', params: [keys] },
    { methodName: 'aria2.tellWaiting', params: [0, 200, keys] },
    { methodName: 'aria2.tellStopped', params: [0, 200, keys] },
    { methodName: 'aria2.getGlobalStat', params: [] },
  ]]);
  const [act, wait, stop, stat] = res.map(r => r[0]);
  $('#stDown').textContent = fmtSize(stat.downloadSpeed) + '/s';
  $('#stUp').textContent = fmtSize(stat.uploadSpeed) + '/s';
  const n = act.length + wait.length;
  $('#bDl').textContent = n; $('#bDl').classList.toggle('hidden', !n);
  if (tab !== 'downloads') return;
  const all = [...act, ...wait, ...stop.slice().reverse()];
  if (!all.length) { $('#dlList').innerHTML = '<div class="empty">কোনো ডাউনলোড নেই</div>'; return; }
  const edit = can('user');
  $('#dlList').innerHTML = all.map(d => {
    const total = +d.totalLength, done = +d.completedLength, speed = +d.downloadSpeed;
    const pct = total ? Math.floor(done / total * 100) : 0;
    const [label, cls] = STATUS[d.status] || [d.status, ''];
    const meta = [`${fmtSize(done)} / ${total ? fmtSize(total) : '?'}`];
    if (d.status === 'active') {
      meta.push(`${fmtSize(speed)}/s`, `ETA ${fmtEta((total - done) / speed)}`, `কানেকশন ${d.connections}`);
      if (d.bittorrent) meta.push(`সিডার ${d.numSeeders}`);
    }
    if (d.errorMessage) meta.push(d.errorMessage);
    const btns = [];
    if (edit) {
      if (d.status === 'active' || d.status === 'waiting') btns.push(`<button class="btn sm" data-a="pause" data-g="${d.gid}">⏸</button>`);
      if (d.status === 'paused') btns.push(`<button class="btn sm" data-a="unpause" data-g="${d.gid}">▶</button>`);
      if (['active', 'waiting', 'paused'].includes(d.status)) btns.push(`<button class="btn sm danger" data-a="remove" data-g="${d.gid}">✖ বাতিল</button>`);
      else btns.push(`<button class="btn sm" data-a="clear" data-g="${d.gid}">🧹</button>`);
      if (d.status === 'error') btns.push(`<button class="btn sm" data-a="retry" data-g="${d.gid}">🔁 আবার</button>`);
    }
    return `<div class="item"><div class="name">${esc(dlName(d))} <span class="pill ${cls}">${label}</span>
      <div class="meta">${esc(meta.join(' · '))}</div>
      <div class="bar ${cls}"><i style="width:${d.status === 'complete' ? 100 : pct}%"></i></div></div>
      <div class="actions">${btns.join('')}</div></div>`;
  }).join('');
}
$('#dlList').onclick = e => {
  const b = e.target.closest('button'); if (!b) return;
  const g = b.dataset.g;
  const acts = {
    pause: () => aria('aria2.pause', [g]),
    unpause: () => aria('aria2.unpause', [g]),
    remove: () => confirm('এই ডাউনলোড বাতিল করবেন?') && aria('aria2.forceRemove', [g]),
    clear: () => aria('aria2.removeDownloadResult', [g]),
    retry: async () => {
      const d = await aria('aria2.tellStatus', [g, ['files', 'dir']]);
      const uris = d.files[0].uris.map(u => u.uri).filter((v, i, s) => s.indexOf(v) === i);
      await aria('aria2.addUri', [uris, { dir: d.dir }]);
      await aria('aria2.removeDownloadResult', [g]);
    },
  };
  run(acts[b.dataset.a]).then(tick);
};

// ---------- shared helpers for file lists
function crumbs(el, path, onGo, rootLabel) {
  const parts = path ? path.split('/') : [];
  let html = `<a data-p="">🏠 ${esc(rootLabel)}</a>`;
  parts.forEach((p, i) => { html += ` / <a data-p="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</a>`; });
  el.innerHTML = html;
  el.onclick = e => { const a = e.target.closest('a'); if (a) onGo(a.dataset.p); };
}
const MEDIA = /\.(mp4|webm|mkv|mov|m4v|mp3|m4a|ogg|wav|flac|jpg|jpeg|png|gif|webp|pdf|txt)$/i;
const THUMBABLE = /\.(jpg|jpeg|png|gif|webp|bmp|mp4|mkv|webm|avi|mov|m4v|flv|wmv|ts)$/i;
const VIDEO = /\.(mp4|mkv|webm|avi|mov|m4v|flv|wmv|ts)$/i;
const ARCHIVE = /\.(zip|rar|7z|tar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)$/i;
function icon(i) {
  if (i.dir) return '📁';
  if (/\.(mp4|mkv|webm|avi|mov|m4v|flv|wmv|ts)$/i.test(i.name)) return '🎬';
  if (/\.(mp3|m4a|ogg|wav|flac)$/i.test(i.name)) return '🎵';
  if (/\.(jpg|jpeg|png|gif|webp|bmp)$/i.test(i.name)) return '🖼';
  if (ARCHIVE.test(i.name)) return '🗜';
  if (/\.(pdf|txt|doc|docx)$/i.test(i.name)) return '📄';
  return '📦';
}

function uploadFile(file, url, listEl, done) {
  const row = document.createElement('div');
  row.className = 'item';
  row.innerHTML = `<div class="name">⬆ ${esc(file.name)}<div class="meta">0%</div><div class="bar"><i style="width:0"></i></div></div>
    <div class="actions"><button class="btn sm danger">✖</button></div>`;
  listEl.prepend(row);
  const xhr = new XMLHttpRequest();
  const started = Date.now();
  xhr.open('PUT', url);
  xhr.setRequestHeader('X-Panel', '1');
  xhr.upload.onprogress = e => {
    if (!e.lengthComputable) return;
    const pct = Math.floor(e.loaded / e.total * 100);
    const speed = e.loaded / ((Date.now() - started) / 1000);
    row.querySelector('i').style.width = pct + '%';
    row.querySelector('.meta').textContent = `${pct}% · ${fmtSize(e.loaded)} / ${fmtSize(e.total)} · ${fmtSize(speed)}/s`
      + (pct === 100 ? ' · শেষ করা হচ্ছে...' : '');
  };
  const finish = (ok, msg) => {
    row.querySelector('.bar').classList.add(ok ? 'ok' : 'bad');
    row.querySelector('.meta').textContent = msg;
    row.querySelector('.actions').innerHTML = '';
    setTimeout(() => row.remove(), ok ? 4000 : 15000);
    if (ok) done(resp);
  };
  let resp = {};
  xhr.onload = () => {
    let d = {}; try { d = JSON.parse(xhr.responseText); } catch (e) {}
    if (xhr.status === 401) showLogin();
    resp = d;
    xhr.status < 300 ? finish(true, 'আপলোড শেষ ✔') : finish(false, 'সমস্যা: ' + (d.error || xhr.statusText));
  };
  xhr.onerror = () => finish(false, 'নেটওয়ার্ক সমস্যা');
  xhr.onabort = () => finish(false, 'বাতিল করা হয়েছে');
  row.querySelector('button').onclick = () => xhr.abort();
  xhr.send(file);
}
function dropZone(el, handler) {
  el.ondragover = e => { e.preventDefault(); el.classList.add('over'); };
  el.ondragleave = () => el.classList.remove('over');
  el.ondrop = e => { e.preventDefault(); el.classList.remove('over'); handler(e.dataTransfer.files); };
}

// ---------- server files
let fPath = '', fItems = [], fSearchMode = false, fSelected = new Set();
let fGrid = false;
try { fGrid = localStorage.getItem('grid') === '1'; } catch (e) {}

async function loadFiles(path = fPath) {
  const d = await run(() => api('/api/files?path=' + enc(path)));
  if (!d) return;
  fPath = d.path;
  fSearchMode = false;
  fItems = d.items.map(i => ({ ...i, path: join(fPath, i.name) }));
  fSelected.clear();
  $('#fSearch').value = '';
  crumbs($('#fCrumbs'), fPath, p => loadFiles(p), 'সার্ভার');
  renderFiles();
}
async function searchAll(q) {
  const d = await run(() => api('/api/search?q=' + enc(q)));
  if (!d) return;
  fSearchMode = true;
  fItems = d.items;
  fSelected.clear();
  $('#fCrumbs').innerHTML = `<a data-p="">← ফিরে যাও</a> · "${esc(q)}" খোঁজার ফলাফল: ${d.items.length}টা${d.truncated ? ' (প্রথম ৩০০টা)' : ''}`;
  $('#fCrumbs').onclick = e => { if (e.target.closest('a')) loadFiles(fPath); };
  renderFiles();
}
function sortedFiles() {
  const q = fSearchMode ? '' : $('#fSearch').value.trim().toLowerCase();
  let items = fItems.filter(i => !q || i.name.toLowerCase().includes(q));
  const s = $('#fSort').value;
  const cmp = s === 'date' ? (a, b) => b.mtime - a.mtime : s === 'size' ? (a, b) => b.size - a.size
    : (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true });
  return items.sort((a, b) => (b.dir - a.dir) || cmp(a, b));
}
function fileActions(i) {
  const p = esc(i.path), dl = '/api/download?path=' + enc(i.path);
  let h = i.dir
    ? `<a class="btn sm" href="/api/zip?path=${enc(i.path)}" title="zip করে নামাও">⬇</a>`
    : `<a class="btn sm" href="${dl}" title="নামাও">⬇</a>`
      + (MEDIA.test(i.name) ? `<a class="btn sm" target="_blank" href="${dl}&inline=1" title="ব্রাউজারে খোলো">▶</a>` : '');
  if (can('user')) {
    if (!i.dir && ARCHIVE.test(i.name)) h += `<button class="btn sm" data-a="extract" data-p="${p}" title="খোলো (extract)">🗜</button>`;
    if (!i.dir && VIDEO.test(i.name)) h += `<button class="btn sm" data-a="subs" data-p="${p}" title="সাবটাইটেল">🎞</button>`;
    h += `<button class="btn sm" data-a="share" data-p="${p}" title="Direct link">🔗</button>`
      + `<button class="btn sm" data-a="tocloud" data-p="${p}" title="Cloud-এ পাঠাও">☁</button>`
      + `<button class="btn sm" data-a="rename" data-p="${p}" data-n="${esc(i.name)}" title="নাম বদলাও">✏</button>`
      + `<button class="btn sm" data-a="move" data-p="${p}" title="অন্য ফোল্ডারে সরাও">📦</button>`
      + `<button class="btn sm danger" data-a="delete" data-p="${p}" title="মুছে ফেলো">🗑</button>`;
  }
  return h;
}
function renderFiles() {
  $('#fView').textContent = fGrid ? '☰' : '▦';
  const items = sortedFiles();
  const sel = i => `<input type="checkbox" data-sel="${esc(i.path)}" ${fSelected.has(i.path) ? 'checked' : ''}>`;
  if (!items.length) $('#fList').innerHTML = '<div class="empty">কিছু নেই</div>';
  else if (fGrid) {
    $('#fList').innerHTML = '<div class="grid">' + items.map(i => {
      const thumb = !i.dir && THUMBABLE.test(i.name)
        ? `<img loading="lazy" src="/api/thumb?path=${enc(i.path)}" alt="" onerror="this.replaceWith(document.createTextNode('${icon(i)}'))">` : icon(i);
      return `<div class="tile">${sel(i)}
        <div class="thumb" data-a="${i.dir ? 'open' : 'view'}" data-p="${esc(i.path)}">${thumb}</div>
        <div class="tname">${esc(i.name)}</div>
        <div class="meta">${i.dir ? 'ফোল্ডার' : fmtSize(i.size)}</div>
        <div class="actions">${fileActions(i)}</div></div>`;
    }).join('') + '</div>';
  } else {
    $('#fList').innerHTML = '<div class="list">' + items.map(i => `<div class="item">${sel(i)}
      <div class="name ${i.dir ? 'link' : ''}" ${i.dir ? `data-a="open" data-p="${esc(i.path)}"` : ''}>
      ${icon(i)} ${esc(fSearchMode ? i.path : i.name)}<div class="meta">${i.dir ? 'ফোল্ডার' : fmtSize(i.size)} · ${fmtDate(i.mtime)}</div></div>
      <div class="actions">${fileActions(i)}</div></div>`).join('') + '</div>';
  }
  updateBulk();
}
function updateBulk() {
  $('#fBulk').classList.toggle('hidden', !fSelected.size);
  $('#fBulkCount').textContent = `${fSelected.size}টা সিলেক্ট করা`;
  $('#fSelectAll').checked = fSelected.size > 0 && fSelected.size === sortedFiles().length;
}
let fFilterTimer;
$('#fSearch').oninput = () => { if (fSearchMode) return; clearTimeout(fFilterTimer); fFilterTimer = setTimeout(renderFiles, 150); };
$('#fSearch').onkeydown = e => { if (e.key === 'Enter' && $('#fSearch').value.trim()) searchAll($('#fSearch').value.trim()); };
$('#fSort').onchange = renderFiles;
$('#fView').onclick = () => { fGrid = !fGrid; try { localStorage.setItem('grid', fGrid ? '1' : '0'); } catch (e) {} renderFiles(); };
$('#fSelectAll').onchange = e => { fSelected = new Set(e.target.checked ? sortedFiles().map(i => i.path) : []); renderFiles(); };

function askCloud(defaultDest) {
  if (!info.remotes.length) { toast('আগে Cloud একাউন্ট কানেক্ট করুন (☁ Cloud ট্যাব)', true); return null; }
  const names = info.remotes.map(r => r.name);
  let remote = names[0];
  if (names.length > 1) {
    remote = prompt(`কোন Cloud একাউন্টে? (${names.join(', ')})`, info.state.autoupload_remote || names[0]);
    if (remote === null) return null;
    if (!names.includes(remote.trim())) { toast('এই নামে কোনো একাউন্ট নেই', true); return null; }
    remote = remote.trim();
  }
  const dest = prompt(`${remote}-এর কোন ফোল্ডারে পাঠাবেন? (খালি = মূল ফোল্ডার)`, defaultDest || '');
  if (dest === null) return null;
  return { remote, dest: cleanFolder(dest) };
}
async function shareLink(p) {
  const days = prompt('লিংক কত দিন চলবে? (1, 7, 30 বা 365)', '7');
  if (days === null) return;
  const d = await post('/api/share', { path: p, days: +days || 7 });
  const url = location.origin + d.url;
  if (await copyText(url)) toast(`Direct link কপি হয়েছে (${d.days} দিন চলবে)`);
  else prompt(`Direct link (${d.days} দিন চলবে):`, url);
}

function moveItems(paths, copy) {
  if (!paths.length) return;
  const dest = prompt(copy ? 'কোন ফোল্ডারে কপি হবে? (খালি = মূল ফোল্ডার)' : 'কোন ফোল্ডারে সরাবে? (খালি = মূল ফোল্ডার)', '');
  if (dest === null) return;
  run(() => post('/api/fs/move', { paths, dest: cleanFolder(dest), copy: !!copy }),
      `${paths.length}টা ${copy ? 'কপি' : 'সরানো'} হয়েছে`)
    .then(() => fSearchMode ? searchAll($('#fSearch').value) : loadFiles());
}
$('#fList').onclick = e => {
  const cb = e.target.closest('[data-sel]');
  if (cb) { cb.checked ? fSelected.add(cb.dataset.sel) : fSelected.delete(cb.dataset.sel); updateBulk(); return; }
  const el = e.target.closest('[data-a]'); if (!el) return;
  const p = el.dataset.p;
  ({
    open: () => loadFiles(p),
    view: () => window.open('/api/download?inline=1&path=' + enc(p), '_blank'),
    share: () => run(() => shareLink(p)),
    extract: () => run(() => post('/api/fs/extract', { path: p }), 'খোলা শুরু হয়েছে, "ট্রান্সফার" ট্যাবে দেখুন').then(tick),
    subs: () => openSubs(p),
    tocloud: () => {
      const c = askCloud(info.state.autoupload_dest); if (!c) return;
      run(() => post('/api/cloud/upload', { path: p, ...c }), 'Cloud-এ পাঠানো শুরু হয়েছে, "ট্রান্সফার" ট্যাবে দেখুন').then(tick);
    },
    rename: () => {
      const name = prompt('নতুন নাম:', el.dataset.n);
      if (name && name !== el.dataset.n) run(() => post('/api/fs/rename', { path: p, name }), 'নাম বদলেছে').then(() => loadFiles());
    },
    delete: () => confirm(`"${p}" মুছে ফেলবেন? এটা আর ফেরত আসবে না।`)
      && run(() => post('/api/fs/delete', { path: p }), 'মুছে ফেলা হয়েছে').then(() => loadFiles()),
    move: () => moveItems([p]),
  })[el.dataset.a]();
};
$('#fBulk').onclick = e => {
  const b = e.target.closest('[data-bulk]'); if (!b) return;
  const paths = [...fSelected];
  ({
    zip: () => { location.href = '/api/zip?' + paths.map(p => 'path=' + enc(p)).join('&'); },
    cloud: () => {
      const c = askCloud(info.state.autoupload_dest); if (!c) return;
      run(async () => { for (const p of paths) await post('/api/cloud/upload', { path: p, ...c }); },
        `${paths.length}টা Cloud-এ পাঠানো শুরু হয়েছে`).then(tick);
    },
    delete: () => confirm(`${paths.length}টা জিনিস মুছে ফেলবেন? এটা আর ফেরত আসবে না।`)
      && run(() => post('/api/fs/delete', { paths }), 'মুছে ফেলা হয়েছে').then(() => fSearchMode ? searchAll($('#fSearch').value) : loadFiles()),
    move: () => moveItems(paths, false),
    copy: () => moveItems(paths, true),
    clear: () => { fSelected.clear(); renderFiles(); },
  })[b.dataset.bulk]();
};
$('#fRefresh').onclick = () => loadFiles();
$('#fMkdir').onclick = () => {
  const name = prompt('নতুন ফোল্ডারের নাম:');
  if (name) run(() => post('/api/fs/mkdir', { dir: fPath, name }), 'ফোল্ডার তৈরি হয়েছে').then(() => loadFiles());
};
function serverUpload(files) {
  const dir = fPath;
  for (const f of files) uploadFile(f, `/api/upload?dir=${enc(dir)}&name=${enc(f.name)}`, $('#fUploads'), () => { if (tab === 'files' && !fSearchMode) loadFiles(); });
}
$('#fUpload').onchange = e => { serverUpload(e.target.files); e.target.value = ''; };
dropZone($('#fDrop'), serverUpload);

// ---------- cloud
let cPath = '', cAdding = false;
const cRemote = () => $('#cRemote').value;
async function loadCloud(path) {
  await loadInfo().catch(() => {});
  const has = info.remotes.length > 0;
  $('#cConnect').classList.toggle('hidden', !(can('admin') && (!has || cAdding)));
  $('#cEmpty').classList.toggle('hidden', has || can('admin'));
  $('#cMain').classList.toggle('hidden', !has);
  $('#cRemove').disabled = !has;
  if (!has) return;
  if (path === undefined) path = cPath;
  const d = await run(() => api(`/api/cloud/list?remote=${enc(cRemote())}&path=${enc(path)}`));
  if (!d) return;
  cPath = d.path;
  const r = info.remotes.find(x => x.name === d.remote) || {};
  $('#cTitle').textContent = '☁ ' + (r.label || '') + ' (' + d.remote + ')';
  crumbs($('#cCrumbs'), cPath, p => loadCloud(p), d.remote);
  const edit = can('user');
  if (!d.items.length) $('#cList').innerHTML = '<div class="empty">ফোল্ডার খালি</div>';
  else $('#cList').innerHTML = d.items.map(i => {
    const p = esc(join(cPath, i.name));
    return `<div class="item"><div class="name ${i.dir ? 'link' : ''}" ${i.dir ? `data-a="open" data-p="${p}"` : ''}>
      ${icon(i)} ${esc(i.name)}<div class="meta">${i.dir ? 'ফোল্ডার' : fmtSize(i.size)} · ${fmtDate(i.mtime)}</div></div>
      <div class="actions">
        ${i.dir ? '' : `<a class="btn sm" href="/api/cloud/download?remote=${enc(d.remote)}&path=${enc(join(cPath, i.name))}&size=${i.size}" title="পিসিতে নামাও">⬇</a>`}
        ${edit ? `<button class="btn sm" data-a="fetch" data-p="${p}" data-d="${i.dir ? 1 : ''}" title="সার্ভারে আনো">🖥</button>
        <button class="btn sm danger" data-a="delete" data-p="${p}" data-d="${i.dir ? 1 : ''}" title="মুছে ফেলো">🗑</button>` : ''}</div></div>`;
  }).join('');
  api('/api/cloud/about?remote=' + enc(d.remote)).then(a => {
    $('#cQuota').textContent = a.total ? `(${fmtSize(a.used)} / ${fmtSize(a.total)} ব্যবহৃত)` : '';
  }).catch(() => {});
}
$('#cRemote').onchange = () => { cPath = ''; loadCloud(''); };
$('#cList').onclick = e => {
  const el = e.target.closest('[data-a]'); if (!el) return;
  const p = el.dataset.p, dir = !!el.dataset.d, remote = cRemote();
  ({
    open: () => loadCloud(p),
    fetch: () => {
      const dest = prompt('সার্ভারের কোন ফোল্ডারে আনবেন? (খালি = মূল ফোল্ডার)', '');
      if (dest === null) return;
      run(() => post('/api/cloud/fetch', { remote, path: p, dir, dest }), 'সার্ভারে আনা শুরু হয়েছে, "ট্রান্সফার" ট্যাবে দেখুন').then(tick);
    },
    delete: () => confirm(`"${p}" মুছবেন?`)
      && run(() => post('/api/cloud/delete', { remote, path: p, dir }), 'মুছে ফেলা হয়েছে').then(() => loadCloud()),
  })[el.dataset.a]();
};
$('#cRefresh').onclick = () => loadCloud();
$('#cMkdir').onclick = () => {
  const name = prompt('নতুন ফোল্ডারের নাম:');
  if (name) run(() => post('/api/cloud/mkdir', { remote: cRemote(), dir: cPath, name }), 'ফোল্ডার তৈরি হয়েছে').then(() => loadCloud());
};
function cloudUpload(files) {
  const dir = cPath, remote = cRemote();
  for (const f of files) uploadFile(f, `/api/cloud/put?remote=${enc(remote)}&dir=${enc(dir)}&name=${enc(f.name)}`, $('#cUploads'), () => { if (tab === 'cloud') loadCloud(); });
}
$('#cUpload').onchange = e => { cloudUpload(e.target.files); e.target.value = ''; };
dropZone($('#cDrop'), cloudUpload);
$('#cAdd').onclick = () => { cAdding = !cAdding; loadCloud(); };
$('#cRemove').onclick = () => {
  const r = cRemote();
  if (r && confirm(`"${r}" একাউন্টটা প্যানেল থেকে সরাবেন? (Cloud-এর ফাইল মুছবে না)`))
    run(() => post('/api/cloud/remove', { remote: r }), 'সরানো হয়েছে').then(() => { cPath = ''; loadCloud(''); });
};
$('#cType').onchange = () => {
  const t = $('#cType').value;
  $('#cOauth').classList.toggle('hidden', t === 'mega');
  $('#cMega').classList.toggle('hidden', t !== 'mega');
  $('#cAuthCmd').textContent = `rclone authorize "${t}"`;
  ['#cStep2', '#cStep3'].forEach(s => $(s).classList.add('hidden'));
};
async function afterConnect(d) {
  toast(`কানেক্ট হয়েছে ✔ (${d.remote})`);
  cAdding = false; cPath = '';
  await loadInfo();
  $('#cRemote').value = d.remote;
  loadCloud('');
}
$('#cStart').onclick = () => run(async () => {
  $('#cStart').disabled = true;
  try {
    const d = await post('/api/cloud/auth/start', { type: $('#cType').value });
    $('#cLink').href = d.url;
    $('#cStep2').classList.remove('hidden');
    $('#cStep3').classList.remove('hidden');
    window.open(d.url, '_blank', 'noopener');
  } finally { $('#cStart').disabled = false; }
});
$('#cFinish').onclick = () => run(async () => {
  $('#cFinish').disabled = true;
  try { const d = await post('/api/cloud/auth/finish', { url: $('#cRedirect').value }); $('#cRedirect').value = ''; await afterConnect(d); }
  finally { $('#cFinish').disabled = false; }
});
$('#cTokenSave').onclick = () => run(async () => {
  const d = await post('/api/cloud/token', { type: $('#cType').value, token: $('#cToken').value });
  $('#cToken').value = '';
  await afterConnect(d);
});
$('#cMegaSave').onclick = () => run(async () => {
  const d = await post('/api/cloud/mega', { user: $('#cMegaUser').value, pass: $('#cMegaPass').value });
  $('#cMegaPass').value = '';
  await afterConnect(d);
});

// ---------- jobs
const KIND = { video: '🎬', archive: '🗜', cloud: '☁' };
async function loadJobs() {
  const d = await api('/api/jobs');
  const running = d.jobs.filter(j => j.status === 'running').length;
  $('#bJobs').textContent = running; $('#bJobs').classList.toggle('hidden', !running);
  if (tab !== 'jobs') return;
  if (!d.jobs.length) { $('#jList').innerHTML = '<div class="empty">কোনো ট্রান্সফার নেই</div>'; return; }
  const S = { running: ['চলছে', ''], done: ['শেষ ✔', 'ok'], error: ['সমস্যা', 'bad'], cancelled: ['বাতিল', 'warn'] };
  $('#jList').innerHTML = d.jobs.map(j => {
    const [label, cls] = S[j.status];
    const title = /^\p{Extended_Pictographic}/u.test(j.title) ? j.title : (KIND[j.kind] || '') + ' ' + j.title;
    return `<div class="item"><div class="name">${esc(title)} <span class="pill ${cls}">${label}</span>
      <div class="meta">${esc(j.error || j.info || (j.status === 'running' ? 'শুরু হচ্ছে...' : ''))} · ${fmtDate(j.started)}</div>
      <div class="bar ${cls}"><i style="width:${j.progress}%"></i></div></div>
      <div class="actions">${j.status === 'running' && can('user') ? `<button class="btn sm danger" data-id="${j.id}">✖ বাতিল</button>` : ''}</div></div>`;
  }).join('');
}
$('#jList').onclick = e => {
  const b = e.target.closest('button[data-id]');
  if (b) run(() => post('/api/jobs/cancel', { id: b.dataset.id })).then(tick);
};
$('#jClear').onclick = () => run(() => post('/api/jobs/clear')).then(tick);

// ---------- RSS
let rssFeeds = [];
async function loadRss() {
  const d = await run(() => api('/api/rss'));
  if (!d) return;
  rssFeeds = d.feeds;
  $('#rDest').value = d.dest;
  $('#rInterval').value = d.interval;
  if (!rssFeeds.length) { $('#rList').innerHTML = '<div class="empty">কোনো ফিড নেই</div>'; return; }
  $('#rList').innerHTML = rssFeeds.map((f, i) => {
    const s = d.status[f.id];
    const st = s ? `শেষ চেক: ${fmtDate(s.checked)} · নতুন: ${s.added}${s.error ? ' · ' + s.error : ''}` : 'এখনো চেক হয়নি';
    return `<div class="item"><div class="name">${esc(f.name || f.url)} ${f.enabled ? '' : '<span class="pill warn">বন্ধ</span>'}
      <div class="meta">${esc(f.url)}</div>
      <div class="meta">ফিল্টার: ${esc(f.filter || '(সব)')} · ${esc(st)}</div></div>
      <div class="actions"><button class="btn sm" data-i="${i}" data-a="toggle">${f.enabled ? '⏸ বন্ধ' : '▶ চালু'}</button>
      <button class="btn sm danger" data-i="${i}" data-a="del">🗑</button></div></div>`;
  }).join('');
}
async function saveFeeds() { await post('/api/rss/save', { feeds: rssFeeds }); await loadRss(); }
$('#rAdd').onclick = () => run(async () => {
  const url = $('#rUrl').value.trim();
  if (!url) throw new Error('RSS লিংক দিন');
  rssFeeds.push({ url, name: $('#rName').value.trim(), filter: $('#rFilter').value.trim(), enabled: true });
  try { await saveFeeds(); } catch (e) { rssFeeds.pop(); throw e; }
  $('#rUrl').value = $('#rName').value = $('#rFilter').value = '';
}, 'ফিড যোগ হয়েছে');
$('#rList').onclick = e => {
  const b = e.target.closest('button[data-i]'); if (!b) return;
  const i = +b.dataset.i;
  if (b.dataset.a === 'toggle') rssFeeds[i].enabled = !rssFeeds[i].enabled;
  else if (!confirm('এই ফিড মুছবেন?')) return;
  else rssFeeds.splice(i, 1);
  run(saveFeeds);
};
$('#rCheck').onclick = () => run(async () => {
  $('#rCheck').disabled = true;
  try { const d = await post('/api/rss/check'); toast(`${d.added}টা নতুন ডাউনলোড যোগ হয়েছে`); await loadRss(); }
  finally { $('#rCheck').disabled = false; }
});
$('#rSaveSettings').onclick = () => run(() => post('/api/settings', { rss_dest: $('#rDest').value, rss_interval_min: +$('#rInterval').value || 15 }), 'সেভ হয়েছে');

// ---------- settings
async function loadSettings() {
  await loadInfo();
  const s = info.state;
  $('#sMe').textContent = `ইউজার: ${info.user} · অনুমতি: ${info.role}`;
  $('#sVersion').textContent = `প্যানেল ভার্সন ${info.version}`;
  if (!can('admin')) return;
  $('#sDown').value = s.speed_down_kb; $('#sUp').value = s.speed_up_kb;
  $('#sSched').checked = s.schedule_enabled; $('#sStart').value = s.schedule_start; $('#sEnd').value = s.schedule_end;
  $('#sGuard').value = s.disk_guard_gb;
  $('#sAuto').checked = s.autoupload; $('#sAutoDest').value = s.autoupload_dest; $('#sAutoMove').checked = s.autoupload_move;
  if (s.autoupload_remote) $('#sAutoRemote').value = s.autoupload_remote;
  $('#sTgToken').value = s.telegram_token; $('#sTgChat').value = s.telegram_chat; $('#sTgNotify').checked = s.telegram_notify;
  $('#sTunnel').innerHTML = info.tunnel
    ? `চালু আছে: <a href="${esc(info.tunnel)}" target="_blank" rel="noopener">${esc(info.tunnel)}</a><br>সার্ভার রিস্টার্ট হলে লিংক বদলে যায়। Telegram চালু থাকলে নতুন লিংক মেসেজে আসবে।`
    : 'বন্ধ আছে। ডোমেইন ছাড়া ফ্রি HTTPS লিংক চাইলে সার্ভারে চালান: <code>curl -fsSL https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/main/install.sh | sudo TUNNEL=1 bash</code>';
  const u = await api('/api/users');
  const R = { viewer: 'viewer (শুধু দেখা)', user: 'user', admin: 'admin' };
  $('#uTable').innerHTML = '<tr><th>ইউজার</th><th>অনুমতি</th><th></th></tr>' + u.users.map(x =>
    `<tr><td>${esc(x.name)}${x.name === info.user ? ' (আপনি)' : ''}</td><td>${R[x.role]}</td>
     <td><button class="btn sm" data-edit="${esc(x.name)}" data-role-v="${x.role}">✏</button>
     ${x.name === info.user ? '' : `<button class="btn sm danger" data-del="${esc(x.name)}">🗑</button>`}</td></tr>`).join('');
}
const SAVE = {
  speed: () => ({ speed_down_kb: +$('#sDown').value || 0, speed_up_kb: +$('#sUp').value || 0,
    schedule_enabled: $('#sSched').checked, schedule_start: $('#sStart').value || '01:00', schedule_end: $('#sEnd').value || '08:00',
    tz_offset_min: -new Date().getTimezoneOffset(), disk_guard_gb: +$('#sGuard').value || 0 }),
  auto: () => {
    if ($('#sAuto').checked && !info.remotes.length) throw new Error('আগে Cloud একাউন্ট কানেক্ট করুন');
    return { autoupload: $('#sAuto').checked, autoupload_remote: $('#sAutoRemote').value, autoupload_dest: $('#sAutoDest').value, autoupload_move: $('#sAutoMove').checked };
  },
  telegram: () => ({ telegram_token: $('#sTgToken').value, telegram_chat: $('#sTgChat').value, telegram_notify: $('#sTgNotify').checked }),
};
$$('[data-save]').forEach(b => b.onclick = () => run(async () => { await post('/api/settings', SAVE[b.dataset.save]()); await loadSettings(); }, 'সেভ হয়েছে'));
$('#sTgTest').onclick = () => run(() => post('/api/telegram/test'), 'মেসেজ পাঠানো হয়েছে, Telegram দেখুন');
$('#pSave').onclick = () => run(async () => {
  await post('/api/password', { old: $('#pOld').value, new: $('#pNew').value });
  $('#pOld').value = $('#pNew').value = '';
}, 'পাসওয়ার্ড বদলেছে');
$('#uSave').onclick = () => run(async () => {
  await post('/api/users/save', { name: $('#uName').value.trim(), password: $('#uPass').value, role: $('#uRole').value });
  $('#uName').value = $('#uPass').value = '';
  await loadSettings();
}, 'ইউজার সেভ হয়েছে');
$('#uTable').onclick = e => {
  const ed = e.target.closest('[data-edit]'), del = e.target.closest('[data-del]');
  if (ed) { $('#uName').value = ed.dataset.edit; $('#uRole').value = ed.dataset.roleV; $('#uPass').focus(); }
  if (del && confirm(`"${del.dataset.del}" ইউজার মুছবেন?`))
    run(() => post('/api/users/delete', { name: del.dataset.del }), 'মুছে ফেলা হয়েছে').then(loadSettings);
};

// ---------- subtitles
const LANGS = [['ben', 'বাংলা'], ['eng', 'English'], ['hin', 'Hindi'], ['urd', 'Urdu'], ['ara', 'Arabic'],
  ['spa', 'Spanish'], ['fra', 'French'], ['kor', 'Korean'], ['jpn', 'Japanese'], ['chi', 'Chinese'], ['und', 'অজানা']];
const LANG_NAME = Object.fromEntries([...LANGS, ['fre', 'French'], ['zho', 'Chinese'], ['bn', 'বাংলা'], ['en', 'English']]);
let subVideo = '', subInfo = null, subNew = [];
const subDir = () => subVideo.includes('/') ? subVideo.slice(0, subVideo.lastIndexOf('/')) : '';
const baseName = p => p.split('/').pop();
function guessLang(name) {
  const n = name.toLowerCase();
  if (/(\.|_|-|\b)(bn|ben|bangla|bengali)(\.|_|-|\b)/.test(n)) return 'ben';
  if (/(\.|_|-|\b)(en|eng|english)(\.|_|-|\b)/.test(n)) return 'eng';
  if (/(\.|_|-|\b)(hi|hin|hindi)(\.|_|-|\b)/.test(n)) return 'hin';
  return 'und';
}
async function openSubs(p) {
  subVideo = p; subNew = [];
  $('#subReplace').checked = false;
  $('#subFile').textContent = p;
  $('#subTracks').innerHTML = '<div class="empty">পড়া হচ্ছে...</div>';
  $('#subNew').innerHTML = '';
  $('#subModal').classList.remove('hidden');
  subInfo = await run(() => api('/api/subs/info?path=' + enc(p)));
  if (!subInfo) { closeSubs(); return; }
  renderSubs();
}
function closeSubs() { $('#subModal').classList.add('hidden'); }
function renderSubs() {
  const t = subInfo.subs;
  $('#subTracks').innerHTML = t.length ? t.map(x => `<div class="item">
      <div class="name"><b>#${x.index + 1}</b> ${esc(LANG_NAME[x.language] || x.language || 'ভাষা লেখা নেই')}
        ${x.title ? '· ' + esc(x.title) : ''} ${x.default ? '<span class="pill ok">ডিফল্ট</span>' : ''} ${x.forced ? '<span class="pill">forced</span>' : ''}
        <div class="meta">${esc(x.codec)}${x.image ? ' (ছবির সাবটাইটেল)' : ''}</div></div>
      <div class="actions">
        <label class="switch"><input type="radio" name="subDefault" value="old:${x.index}"> ডিফল্ট</label>
        <label class="switch"><input type="checkbox" data-rm="${x.index}"> মুছো</label>
        ${x.image ? '' : `<button class="btn sm" data-extract="${x.index}" title=".srt ফাইল হিসেবে আলাদা করো">⬇ .srt</button>`}
      </div></div>`).join('')
    : '<div class="empty">এই ভিডিওতে আলাদা (soft) সাবটাইটেল নেই। ছবির ভেতরে লেখা থাকলে সেটা মোছা যায় না।</div>';
  $('#subPick').innerHTML = subInfo.files.length
    ? subInfo.files.map(f => `<option value="${esc(f)}">${esc(baseName(f))}</option>`).join('')
    : '<option value="">এই ফোল্ডারে কোনো সাবটাইটেল ফাইল নেই</option>';
  renderSubNew();
}
function renderSubNew() {
  $('#subNew').innerHTML = subNew.map((n, i) => `<div class="item">
      <div class="name">➕ ${esc(baseName(n.path))}</div>
      <div class="actions">
        <select data-lang="${i}">${LANGS.map(([c, l]) => `<option value="${c}" ${c === n.lang ? 'selected' : ''}>${l}</option>`).join('')}</select>
        <input type="text" data-title="${i}" placeholder="নাম (ঐচ্ছিক)" value="${esc(n.title)}" style="width:120px">
        <label class="switch"><input type="radio" name="subDefault" value="new:${i}"> ডিফল্ট</label>
        <button class="btn sm danger" data-unadd="${i}">✖</button>
      </div></div>`).join('');
  const burn = subInfo.subs.map(x => `<option value="track:${x.index}">ভিডিওর #${x.index + 1} (${esc(LANG_NAME[x.language] || x.language || '?')})</option>`)
    .concat([...new Set([...subNew.map(n => n.path), ...subInfo.files])].map(f => `<option value="file:${esc(f)}">ফাইল: ${esc(baseName(f))}</option>`));
  $('#subBurnSrc').innerHTML = burn.join('') || '<option value="">আগে একটা সাবটাইটেল যোগ করুন</option>';
}
function addSubFile(path) {
  if (!path || subNew.some(n => n.path === path)) return;
  subNew.push({ path, lang: guessLang(baseName(path)), title: '' });
  renderSubNew();
}
$('#subClose').onclick = closeSubs;
$('#subModal').onclick = e => { if (e.target.id === 'subModal') closeSubs(); };
$('#subPickAdd').onclick = () => addSubFile($('#subPick').value);
$('#subUpload').onchange = e => {
  const f = e.target.files[0]; e.target.value = '';
  if (!f) return;
  const dir = subDir();
  uploadFile(f, `/api/upload?dir=${enc(dir)}&name=${enc(f.name)}`, $('#subUploads'), d => {
    const path = join(dir, d.name || f.name);
    if (!subInfo.files.includes(path)) subInfo.files.push(path);
    addSubFile(path);
  });
};
$('#subNew').oninput = e => {
  const t = e.target;
  if (t.dataset.lang) subNew[+t.dataset.lang].lang = t.value;
  if (t.dataset.title) subNew[+t.dataset.title].title = t.value;
};
$('#subNew').onclick = e => {
  const b = e.target.closest('[data-unadd]');
  if (b) { subNew.splice(+b.dataset.unadd, 1); renderSubNew(); }
};
$('#subTracks').onclick = e => {
  const b = e.target.closest('[data-extract]');
  if (b) run(() => post('/api/subs/extract', { path: subVideo, index: +b.dataset.extract }), '.srt ফাইল বানানো শুরু হয়েছে, ভিডিওর পাশেই পাবেন').then(tick);
};
$('#subSave').onclick = () => run(async () => {
  const remove = $$('#subTracks [data-rm]:checked').map(c => +c.dataset.rm);
  const d = ($('input[name=subDefault]:checked') || {}).value || 'keep';
  const def = d === 'keep' ? {} : d === 'off' ? { type: 'off' } : { type: d.split(':')[0], index: +d.split(':')[1] };
  if (def.type === 'old' && remove.includes(def.index)) throw new Error('যেটা মুছছেন সেটাকে ডিফল্ট করা যাবে না');
  if ($('#subReplace').checked && !confirm('আসল ভিডিও ফাইলটা বদলে যাবে। চালিয়ে যাবেন?')) return;
  await post('/api/subs/edit', { path: subVideo, remove, add: subNew.map(n => ({ path: n.path, lang: n.lang === 'und' ? '' : n.lang, title: n.title })),
    default: def, replace: $('#subReplace').checked });
  closeSubs();
  toast('সাবটাইটেলের কাজ শুরু হয়েছে, "ট্রান্সফার" ট্যাবে দেখুন');
  tick();
});
$('#subBurn').onclick = () => run(async () => {
  const v = $('#subBurnSrc').value;
  if (!v) throw new Error('আগে একটা সাবটাইটেল যোগ করুন');
  const [type, ...rest] = v.split(':');
  const val = rest.join(':');
  await post('/api/subs/burn', { path: subVideo, quality: $('#subBurnQ').value,
    source: type === 'track' ? { type, index: +val } : { type, path: val } });
  closeSubs();
  toast('সাবটাইটেল বসানো শুরু হয়েছে। অনেক সময় লাগতে পারে, "ট্রান্সফার" ট্যাবে progress দেখুন');
  tick();
});

// ---------- polling
let busy = false;
async function tick() {
  if (busy || $('#app').classList.contains('hidden')) return;
  busy = true;
  try {
    await Promise.all([
      loadDownloads().catch(e => { if (tab === 'downloads') $('#dlList').innerHTML = `<div class="empty">⚠ ${esc(e.message)}</div>`; }),
      loadJobs().catch(() => {}),
    ]);
  } finally { busy = false; }
}
async function start() {
  try { await loadInfo(); } catch (e) { return; }
  showApp();
  updateDlForm();
  let saved = 'downloads';
  try { saved = localStorage.getItem('tab') || saved; } catch (e) {}
  setTab(saved);
}
setInterval(tick, 2000);
setInterval(() => { if (!$('#app').classList.contains('hidden')) loadInfo().catch(() => {}); }, 30000);
start();
