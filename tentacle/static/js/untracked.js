// Tentacle - Library → Not in Radarr/Sonarr (admins, #871)
//
// Folders in the movie and show library that Radarr/Sonarr don't know about
// (a manual copy, an old server's library), each with its best TMDB match and
// how sure it is. Add tells Radarr/Sonarr to use the folder as it is: the
// default quality profile, no search; nothing is downloaded, moved or renamed.
// Pick when the match is in doubt. Also shown on the last setup step.
//
// Folder and title names come from disk and TMDB: text, never markup (every
// value goes through escHtml).

const untr = { data: null, timer: null, busy: false, pick: null };

function _untrSize(bytes) {
  if (!bytes) return '';
  const gb = bytes / 1e9;
  return gb >= 1 ? `${gb.toFixed(gb >= 100 ? 0 : 1)} GB` : `${Math.max(1, Math.round(bytes / 1e6))} MB`;
}

function _untrName(kind) { return kind === 'movie' ? 'Radarr' : 'Sonarr'; }

function untrSetBadge(n) {
  const b = document.getElementById('lib-untracked-badge');
  if (!b) return;
  b.textContent = n;
  b.style.display = n ? '' : 'none';
}

// Library opened: the tab's count (a stale list is checked again in the background)
async function untrRefreshBadge() {
  if (!state.currentUser?.is_admin) return;
  try {
    const d = await api('/api/library/untracked');
    untrSetBadge(d.running ? 0 : d.count);
  } catch (_) { /* the tab shows the reason */ }
}

async function loadUntracked(force) {
  if (!state.currentUser?.is_admin) return;
  clearTimeout(untr.timer);
  try {
    untr.data = await api(force ? '/api/library/untracked/scan' : '/api/library/untracked',
                          force ? { method: 'POST' } : {});
    if (force && !untr.data.options) untr.data.options = (await api('/api/library/untracked')).options;
  } catch (e) {
    const box = document.getElementById('untracked-list');
    if (box) box.innerHTML = `<div class="wm-meta">${escHtml(e.message)}</div>`;
    return;
  }
  untrSetBadge(untr.data.running ? 0 : untr.data.count);
  renderUntracked();
  if (untr.data.running) untr.timer = setTimeout(() => loadUntracked(false), 2000);
}

function _untrRow(x) {
  const m = x.match;
  const poster = m?.poster_path
    ? `<img alt="" src="${escHtml(_imgUrl(m.poster_path, 'w92'))}" class="wm-poster" loading="lazy" onerror="this.style.visibility='hidden'">`
    : '<div class="wm-poster"></div>';
  const where = `${escHtml(x.folder)}${x.size ? ` · ${_untrSize(x.size)}` : ''}`;
  let matchLine, action;
  if (x.elsewhere) {
    matchLine = `${escHtml(m.title)}${m.year ? ` (${escHtml(m.year)})` : ''} · ${_untrName(x.kind)} already has it in another folder: <span class="mono">${escHtml(x.elsewhere)}</span>`;
    action = '';
  } else if (!m) {
    matchLine = `No match found (${escHtml(x.how)})`;
    action = `<button class="btn btn-secondary btn-sm" data-untr="pick">Pick…</button>`;
  } else {
    const sure = x.confidence >= untr.data.sure_at && !x.ambiguous;
    matchLine = `→ <strong>${escHtml(m.title)}${m.year ? ` (${escHtml(m.year)})` : ''}</strong> · `
      + `<span class="${sure ? 'untr-sure' : 'untr-unsure'}">${x.confidence}% sure</span> · by ${escHtml(x.how)}`
      + (x.ambiguous ? '' : ` · <a href="#" class="untr-other" data-untr="pick">Not this one?</a>`);
    action = x.ambiguous
      ? `<button class="btn btn-primary btn-sm" data-untr="pick">Pick…</button>`
      : `<button class="btn btn-primary btn-sm" data-untr="add">Add to ${_untrName(x.kind)}</button>`;
  }
  return `<div class="wm-row" data-kind="${escHtml(x.kind)}" data-folder="${escHtml(x.folder)}">
    ${poster}
    <div class="wm-info">
      <div class="wm-title">${x.kind === 'movie' ? 'Film' : 'Show'} · ${where}</div>
      <div class="wm-meta">${matchLine}</div>
    </div>
    <div class="wm-actions">${action}</div>
  </div>`;
}

function renderUntracked() {
  const d = untr.data;
  const head = document.getElementById('untracked-head');
  const box = document.getElementById('untracked-list');
  if (!d || !head || !box) return;
  const opts = d.options || {};
  document.getElementById('untr-monitored').checked = !!opts.monitored;
  document.getElementById('untr-auto').checked = !!opts.auto_add;
  const problems = (d.problems || []).map(p => `<div class="untr-problem">${escHtml(p)}</div>`).join('');
  if (d.running) {
    const p = d.progress;
    head.innerHTML = `<div class="untr-count">Looking through your library…${p && p.total ? ` ${p.done} of ${p.total} folders` : ''}</div>`;
    box.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
    return;
  }
  const n = d.count;
  head.innerHTML = `
    <div class="untr-count">${n ? `${n} folder${n === 1 ? '' : 's'} in your library ${n === 1 ? 'is' : 'are'} not in Radarr/Sonarr`
                                : 'Every folder in your library is in Radarr/Sonarr'}</div>
    ${n ? `<div class="wm-meta">Jellyfin plays them, but Radarr/Sonarr can't upgrade them or find missing episodes, and Tentacle may offer them for download again.
      <strong>Add</strong> tells Radarr/Sonarr to use the folder as it is: nothing is downloaded, moved or renamed.</div>` : ''}
    ${problems}
    <div class="untr-bar">
      ${d.sure ? `<button class="btn btn-primary btn-sm" data-untr="sure">Add all ${d.sure} that are ${d.sure_at}%+ sure</button>` : ''}
      <button class="btn btn-secondary btn-sm" data-untr="rescan">Check again</button>
      ${d.scanned_at ? `<span class="wm-meta">Checked ${escHtml(timeAgo(new Date(d.scanned_at)))}</span>` : ''}
    </div>`;
  box.innerHTML = n ? d.items.map(_untrRow).join('') : '';
}

function _untrItem(row) {
  return untr.data?.items.find(i => i.kind === row.dataset.kind && i.folder === row.dataset.folder);
}

async function untrAdd(kind, folder, tmdbId) {
  if (untr.busy) return;
  untr.busy = true;
  try {
    const monitored = document.getElementById('untr-monitored').checked;
    const r = await api('/api/library/untracked/add', { method: 'POST', body: { kind, folder, tmdb_id: tmdbId || null, monitored } });
    toast(r.outcome === 'exists' ? `${_untrName(kind)} already had it` : `Added to ${_untrName(kind)} with its folder as it is`);
    untr.data.items = untr.data.items.filter(i => !(i.kind === kind && i.folder === folder));
    untr.data.count = r.count;
    untr.data.sure = untr.data.items.filter(i => i.match && !i.elsewhere && !i.ambiguous && i.confidence >= untr.data.sure_at).length;
    untrSetBadge(r.count);
    renderUntracked();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    untr.busy = false;
  }
}

async function untrAddSure() {
  const d = untr.data;
  if (!d?.sure || untr.busy) return;
  if (!confirm(`Add ${d.sure} folder${d.sure === 1 ? '' : 's'} to Radarr/Sonarr as they are? Nothing is downloaded, moved or renamed.`)) return;
  untr.busy = true;
  try {
    const r = await api('/api/library/untracked/add-sure', { method: 'POST', body: { monitored: document.getElementById('untr-monitored').checked } });
    toast(`Added ${r.added}${r.already_exists ? `, ${r.already_exists} already there` : ''}${r.failed ? `, ${r.failed} not added: ${r.detail || ''}` : ''}`, r.failed ? 'error' : undefined);
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    untr.busy = false;
  }
  loadUntracked(false);
}

// ── Pick: the person chooses the title ──
function untrOpenPick(item) {
  untr.pick = item;
  document.getElementById('untr-pick-folder').textContent = item.folder;
  const q = document.getElementById('untr-pick-q');
  q.value = item.title ? `${item.title}${item.year ? ` ${item.year}` : ''}` : item.folder;
  _untrPickList(item.candidates || []);
  showModal('untr-pick-modal');
}

function _untrPickList(cands) {
  const box = document.getElementById('untr-pick-list');
  box.innerHTML = cands.length ? cands.map(c => `
    <div class="wm-row">
      ${c.poster_path ? `<img alt="" src="${escHtml(_imgUrl(c.poster_path, 'w92'))}" class="wm-poster" loading="lazy" onerror="this.style.visibility='hidden'">` : '<div class="wm-poster"></div>'}
      <div class="wm-info"><div class="wm-title">${escHtml(c.title)}${c.year ? ` (${escHtml(c.year)})` : ''}</div>
        <div class="wm-meta">${c.confidence}% like the folder name</div></div>
      <div class="wm-actions"><button class="btn btn-primary btn-sm" data-untr-pick="${Number(c.tmdb_id)}">This one</button></div>
    </div>`).join('') : '<div class="wm-meta">Nothing found. Try the title as TMDB has it, with the year.</div>';
}

async function untrPickSearch() {
  const item = untr.pick;
  const q = document.getElementById('untr-pick-q').value.trim();
  if (!item || !q) return;
  try {
    const r = await api(`/api/library/untracked/search?kind=${encodeURIComponent(item.kind)}&q=${encodeURIComponent(q)}`);
    _untrPickList(r.candidates || []);
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function untrSaveOptions() {
  try {
    untr.data.options = await api('/api/library/untracked/options', { method: 'PUT', body: {
      monitored: document.getElementById('untr-monitored').checked,
      auto_add: document.getElementById('untr-auto').checked,
    } });
  } catch (e) {
    toast(e.message, 'error');
  }
}

// The last setup step: one line when the library has such folders
async function untrSetupNote() {
  const note = document.getElementById('setup-untracked-note');
  if (!note) return;
  let d;
  try { d = await api('/api/library/untracked'); } catch (_) { return; }
  if (d.running) { setTimeout(untrSetupNote, 2000); return; }
  if (!d.count) { note.style.display = 'none'; return; }
  document.getElementById('setup-untracked-count').textContent =
    `${d.count} folder${d.count === 1 ? '' : 's'} in your library ${d.count === 1 ? 'is' : 'are'} not in Radarr/Sonarr yet.`;
  note.style.display = '';
}

function untrOpenFromSetup() {
  // Full reload (as Done does) straight onto Library → Not in Radarr/Sonarr
  window.location.hash = '#library/untracked';
  window.location.reload();
}

document.addEventListener('DOMContentLoaded', () => {
  const panel = document.getElementById('lib-tab-untracked');
  panel?.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-untr]');
    if (!btn) return;
    ev.preventDefault();
    const act = btn.dataset.untr;
    if (act === 'rescan') return loadUntracked(true);
    if (act === 'sure') return untrAddSure();
    const row = btn.closest('.wm-row');
    const item = row && _untrItem(row);
    if (!item) return;
    if (act === 'add') untrAdd(item.kind, item.folder, null);
    if (act === 'pick') untrOpenPick(item);
  });
  panel?.addEventListener('change', ev => {
    if (ev.target.id === 'untr-monitored' || ev.target.id === 'untr-auto') untrSaveOptions();
  });
  const modal = document.getElementById('untr-pick-modal');
  modal?.addEventListener('click', ev => {
    const pick = ev.target.closest('[data-untr-pick]');
    if (pick && untr.pick) {
      closeModal('untr-pick-modal');
      untrAdd(untr.pick.kind, untr.pick.folder, Number(pick.dataset.untrPick));
    }
    if (ev.target.closest('#untr-pick-go')) untrPickSearch();
    if (ev.target.closest('#untr-pick-close')) closeModal('untr-pick-modal');
  });
  document.getElementById('untr-pick-q')?.addEventListener('keydown', ev => { if (ev.key === 'Enter') untrPickSearch(); });
  document.getElementById('setup-untracked-open')?.addEventListener('click', ev => { ev.preventDefault(); untrOpenFromSetup(); });
});
