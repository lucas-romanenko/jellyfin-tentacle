// ══════════════════════════════════════════════════════════════════════════
// ── MUSIC MODULE ─────────────────────────────────────────────────────────
// Discover search (artists, albums, songs), album / artist / song pages, and
// the Library's Music tab. Everything here stays hidden while the module is
// off: the body gets .music-on only when /api/music/config says so.
// ══════════════════════════════════════════════════════════════════════════

const musicState = { config: null, libFilter: 'all', highlight: null, seq: 0 };

async function loadMusicConfig() {
  try {
    musicState.config = await api('/api/music/config');
  } catch (_) {
    musicState.config = { enabled: false, players: [] };
  }
  document.body.classList.toggle('music-on', !!musicState.config.enabled);
  // Leaving the module off while a music view is open: fall back to the normal views.
  if (!musicState.config.enabled && typeof _discoverType !== 'undefined' && _discoverType === 'music') {
    setDiscoverType('movies', document.querySelector('[data-disctype="movies"]'));
  }
  return musicState.config;
}

function musicOn() { return !!(musicState.config && musicState.config.enabled); }

const _MUSIC_STATUS = {
  in_library: ['badge-green', 'In library'],
  downloading: ['badge-accent', 'Downloading'],
  wanted: ['badge-blue', 'Wanted'],
  needs_review: ['badge-amber', 'Needs review'],
};

function _musicBadge(item, fallback) {
  const s = _MUSIC_STATUS[item.status];
  if (!s) return `<span class="badge" style="font-size:9px;padding:1px 5px;background:var(--bg3);color:var(--text3)">${escapeAttr(fallback || item.type || 'Album')}</span>`;
  const label = item.status === 'downloading' && item.progress != null ? `Downloading ${item.progress}%` : s[1];
  return `<span class="badge ${s[0]}" style="font-size:9px;padding:1px 5px">${label}</span>`;
}

function _musicDuration(ms) {
  if (!ms) return '';
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function _musicCover(url, size) {
  const style = `width:${size}px;height:${size}px;border-radius:6px;object-fit:cover;flex-shrink:0;background:var(--bg3)`;
  return url
    ? `<img src="${escapeAttr(url)}" style="${style}" loading="lazy" onerror="this.outerHTML='<div style=&quot;${style};display:flex;align-items:center;justify-content:center;color:var(--text3)&quot;>♫</div>'">`
    : `<div style="${style};display:flex;align-items:center;justify-content:center;color:var(--text3)">♫</div>`;
}

function _musicAlbumCard(a, opts = {}) {
  const canRequest = a.status === 'available' && !opts.noRequest;
  const addBtn = canRequest
    ? `<button onclick="event.stopPropagation();requestMusicAlbum('${escapeJS(a.mbid)}', this)" class="lib-card-add-btn" title="Request this album">+</button>`
    : '';
  const cover = a.cover
    ? `<img src="${escapeAttr(a.cover)}" loading="lazy" onerror="this.outerHTML='<div class=\\'lib-card-poster-placeholder\\'>♫</div>'">`
    : '<div class="lib-card-poster-placeholder">♫</div>';
  const sub = opts.meta != null ? opts.meta
    : opts.showArtist === false ? (a.year || '—') : `${escapeAttr(a.artist || '')}${a.year ? ' · ' + a.year : ''}`;
  return `<div class="lib-card music" onclick="openMusicAlbum('${escapeJS(a.mbid)}')">
      <div class="lib-card-poster">${cover}<div class="lib-card-source">${_musicBadge(a)}</div>${addBtn}</div>
      <div class="lib-card-info">
        <div class="lib-card-title" title="${escapeAttr(a.title)}">${escapeAttr(a.title)}</div>
        <div class="lib-card-meta">${sub}</div>
      </div>
    </div>`;
}

// ── Discover → Music: trending, new releases, top of all time, Spotify ────

const _MUSIC_TABS = [['trending', 'Trending'], ['new', 'New releases'], ['lists', 'Lists'], ['alltime', 'Top of all time'],
  ['spotify', 'From Spotify']];
Object.assign(musicState, { discover: null, dTab: 'trending', dSub: { trending: 'artists', new: 'all', alltime: '', lists: '' },
  hideOwned: false, dTimer: null, dSeq: 0, listCache: {} });

function _musicDiscoverVisible() {
  const browse = document.getElementById('discover-tab-browse');
  return typeof _discoverType !== 'undefined' && _discoverType === 'music' && !_discoverSearchQuery
    && browse && browse.offsetParent !== null;
}

async function musicDiscoverHome() {
  const tabs = document.getElementById('discover-section-tabs');
  if (tabs) {
    tabs.innerHTML = _MUSIC_TABS.map(([id, label]) =>
      `<button class="discover-sec-tab" data-mtab="${id}" onclick="musicDiscoverTab('${id}')">${label}</button>`).join('');
  }
  if (!musicState.discover) {
    document.getElementById('discover-grid').innerHTML =
      '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Loading…</div>';
  } else {
    musicDiscoverTab(musicState.dTab);
  }
  await _loadMusicDiscover();
}

async function _loadMusicDiscover() {
  const seq = ++musicState.dSeq;
  let data;
  try {
    data = await api('/api/music/discover');
  } catch (e) {
    data = { error: e.message };
  }
  if (seq !== musicState.dSeq) return;
  musicState.discover = data;
  if (_musicDiscoverVisible()) musicDiscoverTab(musicState.dTab);
  clearTimeout(musicState.dTimer);
  const busy = data.building && (data.building.trending || data.building.all_time || data.building.lists)
    || ((data.spotify || {}).imports || []).some(i => i.status === 'resolving');
  // Keep refreshing while something is being built, as long as the page is open.
  if (busy) musicState.dTimer = setTimeout(() => { if (_musicDiscoverVisible()) _loadMusicDiscover(); }, 8000);
}

function musicDiscoverTab(id) {
  musicState.dTab = id;
  document.querySelectorAll('#discover-section-tabs .discover-sec-tab').forEach(btn => {
    const active = btn.getAttribute('data-mtab') === id;
    btn.style.color = active ? 'var(--text)' : 'var(--text3)';
    btn.style.borderBottomColor = active ? 'var(--accent)' : 'transparent';
  });
  const d = musicState.discover;
  const grid = document.getElementById('discover-grid');
  const pills = document.getElementById('discover-streaming-pills');
  pills.style.display = 'none';
  pills.innerHTML = '';
  if (!d) return;
  if (d.error) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>${escapeAttr(d.error)}</p></div>`;
    return;
  }
  ({ trending: _musicTrending, new: _musicNew, lists: _musicLists, alltime: _musicAllTime, spotify: _musicSpotify })[id](d, grid, pills);
}

function _musicPills(pills, items, active, handler) {
  pills.style.display = 'flex';
  pills.classList.add('music-pills');   // one scrolling row on phones
  pills.innerHTML = items.map(([id, label, count]) =>
    `<button class="lib-list-pill${id === active ? ' active' : ''}" onclick="${handler}('${escapeJS(id)}')">${escapeAttr(label)}${count != null ? ` <span style="opacity:.7">${count}</span>` : ''}</button>`).join('');
}

function musicDiscoverSub(value) {
  musicState.dSub[musicState.dTab] = value;
  musicDiscoverTab(musicState.dTab);
}

function _musicNote(html) {
  return `<div class="music-note">${html}</div>`;
}

function _musicAgo(ts) {
  if (!ts) return '';
  const h = Math.round((Date.now() / 1000 - ts) / 3600);
  return h < 1 ? 'updated just now' : h < 48 ? `updated ${h} h ago` : `updated ${Math.round(h / 24)} days ago`;
}

function _musicEmpty(text) {
  return `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>${text}</p></div>`;
}

function _musicBuilding(d, which, what) {
  const err = (d.errors || {})[which];
  if (err) return _musicNote(`<span style="color:var(--red)">Couldn't build ${what}: ${escapeAttr(err)}</span>`);
  if ((d.building || {})[which]) return _musicNote(`<span class="toast-spinner"></span> Building ${what} in the background…`);
  return '';
}

function _musicArtistCard(a) {
  const pic = a.picture
    ? `<img class="music-artist-pic" src="${escapeAttr(a.picture)}" loading="lazy" onerror="this.outerHTML='<div class=\\'music-artist-pic\\'>${escapeAttr((a.name || '?').charAt(0))}</div>'">`
    : `<div class="music-artist-pic">${escapeAttr((a.name || '?').charAt(0))}</div>`;
  return `<div class="music-artist-card" onclick="openMusicArtist('${escapeJS(a.mbid)}')">${pic}
      <div class="music-artist-name" title="${escapeAttr(a.name)}">${escapeAttr(a.name)}</div>
      ${a.in_library ? '<span class="badge badge-green" style="font-size:9px;padding:1px 5px">In Lidarr</span>' : ''}
    </div>`;
}

function _musicSongCard(s) {
  const a = s.album;
  const addBtn = a.status === 'available'
    ? `<button onclick="event.stopPropagation();requestMusicAlbum('${escapeJS(a.mbid)}', this)" class="lib-card-add-btn" title="Request ${escapeAttr(a.title)}">+</button>` : '';
  const art = s.artwork || a.cover;
  const cover = art ? `<img src="${escapeAttr(art)}" loading="lazy" onerror="this.outerHTML='<div class=\\'lib-card-poster-placeholder\\'>♪</div>'">`
    : '<div class="lib-card-poster-placeholder">♪</div>';
  return `<div class="lib-card music" onclick="openMusicSong('${escapeJS(s.title)}', '${escapeJS(s.artist_mbid)}')">
      <div class="lib-card-poster">${cover}${a.status !== 'available' ? `<div class="lib-card-source">${_musicBadge(a)}</div>` : ''}${addBtn}</div>
      <div class="lib-card-info">
        <div class="lib-card-title" title="${escapeAttr(s.title)}">${escapeAttr(s.title)}</div>
        <div class="lib-card-meta">${escapeAttr(s.artist)}</div>
        <div class="lib-card-meta" title="${escapeAttr(a.title)}">from ${escapeAttr(a.title)}${a.year ? ' (' + a.year + ')' : ''}</div>
      </div>
    </div>`;
}

function _musicDate(iso) {
  if (!iso) return '';
  const d = new Date(iso + 'T12:00:00');
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function _musicTrending(d, grid, pills) {
  const t = d.trending || {};
  const sub = musicState.dSub.trending;
  _musicPills(pills, [['artists', 'Artists', (t.artists || []).length], ['songs', 'Songs', (t.songs || []).length]],
    sub, 'musicDiscoverSub');
  let html = _musicBuilding(d, 'trending', "the charts");
  if (t.built) html += _musicNote(`Apple Music's charts for ${escapeAttr((d.country || '').toUpperCase())} · ${_musicAgo(t.built)}`);
  const items = sub === 'songs' ? (t.songs || []).map(_musicSongCard) : (t.artists || []).map(_musicArtistCard);
  grid.innerHTML = html + (items.join('') || (t.built ? _musicEmpty('Nothing to show.') : ''));
}

function _musicNew(d, grid, pills) {
  const n = d.new || {};
  const sub = musicState.dSub.new;
  _musicPills(pills, [['all', 'All', (n.releases || []).length], ['yours', 'From your artists', (n.yours || []).length]]
    .concat((n.genres || []).map(([g, c]) => [g, g, c])), sub, 'musicDiscoverSub');
  let html = _musicBuilding(d, 'trending', 'new releases');
  let items;
  if (sub === 'yours') {
    html += _musicNote('New and announced albums by artists already in your library.');
    items = n.yours || [];
  } else {
    html += _musicNote(`Albums out in the last 8 weeks that are charting on Apple Music (${escapeAttr((d.country || '').toUpperCase())}).`);
    items = sub === 'all' ? (n.releases || []) : (n.releases || []).filter(a => (a.genres || []).includes(sub));
  }
  const cards = items.map(a => _musicAlbumCard(a, { meta: `${escapeAttr(a.artist || '')} · ${a.upcoming ? 'out ' : ''}${_musicDate(a.date)}` }));
  grid.innerHTML = html + (cards.join('') || _musicEmpty(sub === 'yours'
    ? 'No new albums from your artists in the last few weeks.' : 'No new albums here yet.'));
}

function _musicAllTime(d, grid, pills) {
  const at = d.all_time || {};
  const genres = at.genres || [];
  let sub = musicState.dSub.alltime;
  if (!genres.find(g => g.name === sub)) sub = musicState.dSub.alltime = genres.length ? genres[0].name : '';
  if (genres.length) _musicPills(pills, genres.map(g => [g.name, g.name, g.albums.length]), sub, 'musicDiscoverSub');
  let html = '';
  if (!at.complete && (d.building || {}).all_time) {
    const p = at.progress || {};
    html += _musicNote(`<span class="toast-spinner"></span> Sorting ListenBrainz's most-listened albums into genres${p.total
      ? `: ${p.done} of ${p.total} checked` : '…'}. MusicBrainz allows one request a second, so the first time takes a while; genres fill in as it goes.`);
  } else {
    html += _musicBuilding(d, 'all_time', 'the all-time list');
  }
  html += `<div class="music-note" style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">
      <span>The most-listened studio albums on ListenBrainz, by genre${at.built ? ' · ' + _musicAgo(at.built) : ''}.</span>
      <label style="display:flex;gap:6px;align-items:center;cursor:pointer"><input type="checkbox" ${musicState.hideOwned ? 'checked' : ''} onchange="musicState.hideOwned=this.checked;musicDiscoverTab('alltime')"> Hide albums I have</label>
    </div>`;
  const current = genres.find(g => g.name === sub);
  const albums = ((current || {}).albums || []).filter(a => !musicState.hideOwned || a.status === 'available');
  grid.innerHTML = html + (albums.map(a => _musicAlbumCard(a)).join('') ||
    (genres.length ? _musicEmpty('You have every album in this list.') : ''));
}

function _musicIsAdmin() {
  return typeof state !== 'undefined' && state.currentUser && state.currentUser.is_admin;
}

function _musicLists(d, grid) {
  const items = (d.lists || {}).items || [];
  if (!items.find(l => l.id === musicState.dSub.lists)) musicState.dSub.lists = items.length ? items[0].id : '';
  const sid = musicState.dSub.lists;
  let html = _musicBuilding(d, 'lists', 'the album lists');
  if (!items.length) {
    grid.innerHTML = html + (d.building && d.building.lists ? '' : _musicEmpty('No album lists yet.')) + _musicListAdmin(null);
    return;
  }
  html += `<div class="music-note music-list-bar">
      <select class="form-input" onchange="musicDiscoverSub(this.value)" style="max-width:420px">
        ${items.map(l => `<option value="${escapeAttr(l.id)}"${l.id === sid ? ' selected' : ''}>${escapeAttr(l.name)} (${l.count})</option>`).join('')}
      </select>
      <label style="display:flex;gap:6px;align-items:center;cursor:pointer"><input type="checkbox" ${musicState.hideOwned ? 'checked' : ''} onchange="musicState.hideOwned=this.checked;musicDiscoverTab('lists')"> Hide albums I have</label>
    </div>`;
  const cached = musicState.listCache[sid];
  if (!cached || Date.now() - cached.at > 60000) {
    grid.innerHTML = html + '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Loading…</div>';
    api(`/api/music/discover/list/${encodeURIComponent(sid)}`).then(data => {
      musicState.listCache[sid] = { at: Date.now(), data };
      if (_musicDiscoverVisible() && musicState.dTab === 'lists' && musicState.dSub.lists === sid) musicDiscoverTab('lists');
    }).catch(e => { grid.innerHTML = html + _musicEmpty(escapeAttr(e.message)); });
    return;
  }
  const albums = cached.data.albums || [];
  const owned = albums.filter(a => a.status !== 'available').length;
  html += _musicNote(`${albums.length} albums, ${owned} of them in your library. From MusicBrainz.`);
  const shown = albums.filter(a => !musicState.hideOwned || a.status === 'available');
  grid.innerHTML = html + (shown.map(a => _musicAlbumCard(a, {
    meta: `#${a.rank} · ${escapeAttr(a.artist || '')}${a.year ? ' · ' + a.year : ''}` })).join('')
    || _musicEmpty('You have every album on this list.')) + _musicListAdmin(sid);
}

function _musicListAdmin(sid) {
  if (!_musicIsAdmin()) return '';
  return `<div class="music-note" style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px">
      <button class="btn btn-secondary btn-sm" onclick="addMusicList(this)">Add a list…</button>
      ${sid ? `<button class="btn btn-secondary btn-sm" onclick="removeMusicList('${escapeJS(sid)}', this)">Remove this list</button>` : ''}
      <span>Any MusicBrainz album list works: find one at musicbrainz.org (search for a series) and paste its link.</span>
    </div>`;
}

async function addMusicList(btn) {
  const link = prompt('Paste a MusicBrainz series link (a list of albums):');
  if (!link) return;
  btn.disabled = true;
  try {
    const r = await api('/api/music/lists', { method: 'POST', body: { series: link } });
    toast(`Adding “${escapeAttr(r.name)}”…`, 'info');
    musicState.dSub.lists = r.id;
    await _loadMusicDiscover();
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
  }
  btn.disabled = false;
}

async function removeMusicList(sid, btn) {
  if (!confirm('Remove this list from Discover?')) return;
  btn.disabled = true;
  try {
    await api(`/api/music/lists/${encodeURIComponent(sid)}`, { method: 'DELETE' });
    delete musicState.listCache[sid];
    musicState.dSub.lists = '';
    await _loadMusicDiscover();
  } catch (e) {
    toast(escapeAttr(e.message), 'error');
    btn.disabled = false;
  }
}

function _musicSpotify(d, grid) {
  const s = d.spotify || {};
  const imports = s.imports || [];
  let html = `<div class="music-note" style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
      <span>Import a Spotify playlist: Tentacle finds the original studio album of every song, and you pick which to request.</span>
      <button class="btn btn-primary btn-sm" onclick="showMusicImport()">Import a playlist</button>
    </div>`;
  if (imports.length) {
    html += '<div class="music-rows">' + imports.map(i => {
      const progress = i.status === 'resolving'
        ? `<div class="music-progress"><div style="width:${i.total ? Math.round(100 * i.done / i.total) : 0}%"></div></div>
           <div class="music-row-sub">Finding albums: ${i.done} of ${i.total} songs</div>
           ${i.error ? `<div class="music-row-sub">${escapeAttr(i.error)}</div>` : ''}`
        : i.status === 'error' ? `<div class="music-row-sub" style="color:var(--red)">${escapeAttr(i.error || 'Failed')}</div>`
        : `<div class="music-row-sub">${i.total} songs → ${i.albums} albums${i.skipped ? ` · ${i.skipped} song${i.skipped === 1 ? '' : 's'} not matched` : ''}</div>`;
      return `<div class="music-row" onclick="openMusicImport(${i.id})">
          <div class="music-row-icon">♫</div>
          <div class="music-row-main"><div class="music-row-title">${escapeAttr(i.name)}</div>${progress}</div>
          <div class="music-lib-actions" onclick="event.stopPropagation()">
            ${i.status === 'error' ? `<button class="btn btn-secondary btn-sm" onclick="retryMusicImport(${i.id}, this)" title="Carry on finding albums">Retry</button>` : ''}
            ${i.refreshable ? `<button class="btn btn-secondary btn-sm" onclick="refreshMusicImport(${i.id}, this)" title="Read the playlist again">Refresh</button>` : ''}
            <button class="btn btn-secondary btn-sm" onclick="deleteMusicImport(${i.id}, this)" title="Forget this playlist">Remove</button>
          </div>
        </div>`;
    }).join('') + '</div>';
  }
  const albums = s.albums || [];
  const missing = albums.filter(a => a.status === 'available');
  if (albums.length) {
    html += `<div class="music-note" style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px">
        <span>Albums from your playlists: ${albums.length}, ${missing.length} not in your library.</span>
      </div>`;
    html += albums.map(a => _musicAlbumCard(a, { meta: `${escapeAttr(a.artist || '')} · ${a.songs.length} song${a.songs.length === 1 ? '' : 's'}` })).join('');
  } else if (!imports.length) {
    html += _musicEmpty('No playlists imported yet.');
  }
  grid.innerHTML = html;
}

// ── Spotify import: dialog, preview, requests ─────────────────────────────

function showMusicImport() {
  _musicModal('Import a Spotify playlist', `
    <div class="form-group">
      <div class="form-label">Public playlist link</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input class="form-input" id="music-import-url" placeholder="https://open.spotify.com/playlist/…" style="flex:1;min-width:220px">
        <button class="btn btn-primary" onclick="startMusicImport(this)">Import</button>
      </div>
      <div class="form-hint">Reads the first 100 songs. No Spotify account or key needed.</div>
    </div>
    <div class="form-group" style="margin-top:14px">
      <div class="form-label">Or an Exportify file (any size, private playlists too)</div>
      <input type="file" id="music-import-file" accept=".csv,text/csv" onchange="startMusicImport(null)">
      <div class="form-hint">Export the playlist at <a href="https://exportify.net" target="_blank" rel="noopener" style="color:var(--accent)">exportify.net</a> and choose the CSV it saves.</div>
    </div>
    <p class="form-hint" style="margin-top:14px">Nothing is requested yet: you'll see the albums first and tick the ones you want.</p>`);
  setTimeout(() => { const i = document.getElementById('music-import-url'); if (i) i.focus(); }, 50);
}

async function startMusicImport(btn) {
  const url = (document.getElementById('music-import-url') || {}).value || '';
  const fileInput = document.getElementById('music-import-file');
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!url.trim() && !file) { toast('Paste a playlist link or choose a file', 'error'); return; }
  const form = new FormData();
  if (file) form.append('file', file); else form.append('url', url.trim());
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/music/imports', { method: 'POST', body: form });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.statusText);
    closeModal('modal-music');
    toast(`Importing “${escapeAttr(body.name)}”: finding albums for ${body.total} songs…`, 'info', 6000);
    musicState.dTab = 'spotify';
    await _loadMusicDiscover();
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    if (btn) btn.disabled = false;
    if (fileInput) fileInput.value = '';
  }
}

async function openMusicImport(id, keepOpen) {
  if (!keepOpen) _musicLoading('Playlist');
  let d;
  try {
    d = await api(`/api/music/imports/${id}`);
  } catch (e) {
    _musicModal('Playlist', `<p style="color:var(--red)">${escapeAttr(e.message)}</p>`);
    return;
  }
  if (keepOpen && !document.getElementById(`music-import-${id}`)) return;   // closed meanwhile
  const available = d.albums.filter(a => a.status === 'available');
  const rows = d.albums.map(a => {
    const can = a.status === 'available';
    const songs = a.songs.slice(0, 3).map(t => `“${escapeAttr(t)}”`).join(', ') + (a.songs.length > 3 ? ` +${a.songs.length - 3}` : '');
    return `<label class="music-check-row">
        <input type="checkbox" value="${escapeAttr(a.mbid)}" ${can ? 'checked' : 'disabled'} onchange="_musicImportCount(${id})">
        ${_musicCover(a.cover, 40)}
        <div class="music-row-main">
          <div class="music-row-title">${escapeAttr(a.title)} <span style="color:var(--text3)">· ${escapeAttr(a.artist || '')}${a.year ? ' · ' + a.year : ''}</span></div>
          <div class="music-row-sub">${songs}</div>
          ${a.refused ? `<div class="music-row-sub" style="color:var(--red)">${escapeAttr(a.refused)}</div>` : ''}
        </div>
        ${_musicBadge(a)}
      </label>`;
  }).join('');
  const progress = d.status === 'resolving'
    ? `<div class="music-progress" style="margin:6px 0 10px"><div style="width:${d.total ? Math.round(100 * d.done / d.total) : 0}%"></div></div>
       <p class="form-hint">Finding albums: ${d.done} of ${d.total} songs. You can request what's found so far.</p>` : '';
  const skipped = d.skipped.length ? `<details style="margin-top:14px"><summary style="cursor:pointer;font-size:13px">${d.skipped.length} song${d.skipped.length === 1 ? '' : 's'} not matched to a studio album</summary>
      <div class="music-rows" style="margin-top:8px">${d.skipped.map(s => `<div class="music-row-sub"><b style="color:var(--text2)">${escapeAttr(s.title)}</b> · ${escapeAttr(s.artist)}: ${escapeAttr(s.reason)}</div>`).join('')}</div></details>` : '';
  _musicModal(d.name, `<div id="music-import-${id}">
      ${d.error ? `<p style="color:${d.status === 'error' ? 'var(--red)' : 'var(--text2)'}">${escapeAttr(d.error)}</p>` : ''}
      ${progress}
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">
        <span class="form-hint" style="margin:0">${d.total} songs → ${d.albums.length} albums, ${available.length} not in your library</span>
        <button class="btn btn-primary btn-sm" id="music-import-go-${id}" onclick="requestMusicImport(${id}, this)" ${available.length ? '' : 'disabled'}>Request ${available.length}</button>
      </div>
      <div class="music-check-list">${rows || '<p class="form-hint">No albums found yet.</p>'}</div>
      ${skipped}
    </div>`);
  if (d.status === 'resolving') setTimeout(() => openMusicImport(id, true), 4000);
}

function _musicImportCount(id) {
  const n = document.querySelectorAll(`#music-import-${id} input[type=checkbox]:checked`).length;
  const btn = document.getElementById(`music-import-go-${id}`);
  if (btn) { btn.textContent = `Request ${n}`; btn.disabled = !n; }
}

async function requestMusicImport(id, btn) {
  const mbids = [...document.querySelectorAll(`#music-import-${id} input[type=checkbox]:checked`)].map(c => c.value);
  if (!mbids.length) return;
  btn.disabled = true;
  try {
    const r = await api(`/api/music/imports/${id}/request`, { method: 'POST', body: { mbids } });
    toast(`Requesting ${r.queued} album${r.queued === 1 ? '' : 's'}, one at a time…`, 'info', 6000);
    closeModal('modal-music');
    setTimeout(_loadMusicDiscover, 3000);
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    btn.disabled = false;
  }
}

async function refreshMusicImport(id, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(`/api/music/imports/${id}/refresh`, { method: 'POST' });
    toast('Reading the playlist again…', 'info');
    await _loadMusicDiscover();
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    if (btn) btn.disabled = false;
  }
}

async function retryMusicImport(id, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(`/api/music/imports/${id}/retry`, { method: 'POST' });
    toast('Carrying on finding albums…', 'info');
    await _loadMusicDiscover();
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    if (btn) btn.disabled = false;
  }
}

async function deleteMusicImport(id, btn) {
  if (!confirm('Forget this playlist? Albums you already requested stay in Lidarr.')) return;
  if (btn) btn.disabled = true;
  try {
    await api(`/api/music/imports/${id}`, { method: 'DELETE' });
    await _loadMusicDiscover();
  } catch (e) {
    toast(escapeAttr(e.message), 'error');
    if (btn) btn.disabled = false;
  }
}

// ── Discover: search ──────────────────────────────────────────────────────

async function musicSearch(query) {
  const grid = document.getElementById('discover-grid');
  const tabs = document.getElementById('discover-section-tabs');
  if (tabs) tabs.innerHTML = '';
  const seq = ++musicState.seq;
  grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text3)"><span class="toast-spinner"></span> Searching MusicBrainz…</div>';
  let data;
  try {
    data = await api(`/api/music/search?q=${encodeURIComponent(query)}`);
  } catch (e) {
    if (seq === musicState.seq) grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>Search failed: ${escapeAttr(e.message)}</p></div>`;
    return;
  }
  if (seq !== musicState.seq || data.stale) return;
  const { artists = [], albums = [], songs = [] } = data;
  if (!artists.length && !albums.length && !songs.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px"><p>No music found for "${escapeAttr(query)}"</p></div>`;
    return;
  }
  let html = '';
  if (artists.length) {
    html += '<div class="music-section-title">Artists</div><div class="music-rows">' + artists.map(a => `
      <div class="music-row" onclick="openMusicArtist('${escapeJS(a.mbid)}')">
        <div class="music-row-icon">${escapeAttr((a.name || '?').charAt(0))}</div>
        <div class="music-row-main">
          <div class="music-row-title">${escapeAttr(a.name)}${a.in_library ? ' <span class="badge badge-green" style="font-size:9px;padding:1px 5px">In Lidarr</span>' : ''}</div>
          <div class="music-row-sub">${escapeAttr([a.type, a.country, a.disambiguation].filter(Boolean).join(' · '))}</div>
        </div>
      </div>`).join('') + '</div>';
  }
  if (albums.length) {
    html += '<div class="music-section-title">Albums</div>' + albums.map(a => _musicAlbumCard(a)).join('');
  }
  if (songs.length) {
    html += '<div class="music-section-title">Songs</div><div class="music-rows">' + songs.map(s => `
      <div class="music-row" onclick="openMusicSong('${escapeJS(s.title)}', '${escapeJS(s.artist_mbid)}')">
        <div class="music-row-icon">♪</div>
        <div class="music-row-main">
          <div class="music-row-title">${escapeAttr(s.title)}</div>
          <div class="music-row-sub">${escapeAttr(s.artist)}${s.length ? ' · ' + _musicDuration(s.length) : ''}</div>
        </div>
        <div class="music-row-hint">Find the original album ›</div>
      </div>`).join('') + '</div>';
  }
  grid.innerHTML = html;
}

// ── Album / artist / song pages (one modal) ──────────────────────────────

function _musicModal(title, bodyHtml) {
  document.getElementById('music-modal-title').textContent = title;
  document.getElementById('music-modal-body').innerHTML = bodyHtml;
  showModal('modal-music');
}

function _musicLoading(title) {
  _musicModal(title, '<div class="loading-state"><div class="spinner"></div></div>');
}

function _trackIsHighlighted(t) {
  const h = musicState.highlight;
  if (!h) return false;
  if (t.recording_mbid && (h.recordings || []).includes(t.recording_mbid)) return true;
  return _musicNorm(t.title) === h.title;
}

function _musicNorm(s) {
  return (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .replace(/[‘’`´']/g, '').replace(/[^\w\s]/g, ' ').split(/\s+/).filter(Boolean).join(' ');
}

function _musicTracklist(discs) {
  if (!discs || !discs.length) return '<div style="color:var(--text3);font-size:12px">No tracklist in MusicBrainz.</div>';
  return discs.map(d => `
    ${discs.length > 1 ? `<div class="music-disc">Disc ${d.position}${d.format ? ' · ' + escapeAttr(d.format) : ''}</div>` : ''}
    <div class="music-tracklist">${(d.tracks || []).map(t => `
      <div class="music-track${_trackIsHighlighted(t) ? ' hl' : ''}">
        <span class="music-track-n">${escapeAttr(t.number || '')}</span>
        <span class="music-track-title">${escapeAttr(t.title)}</span>
        <span class="music-track-len">${_musicDuration(t.length)}</span>
      </div>`).join('')}</div>`).join('');
}

function _musicActions(a) {
  const isAdmin = state.currentUser && state.currentUser.is_admin;
  const parts = [];
  // An ambiguous album with tracklist options is requested with one of them (below);
  // one MusicBrainz can't date is requested as is and its edition picked once Lidarr has it.
  if (a.status === 'available' && !(a.review && (a.review.options || []).length)) {
    parts.push(`<button class="btn btn-primary" onclick="requestMusicAlbum('${escapeJS(a.mbid)}', this)">Request</button>`);
  }
  (a.players || []).forEach(p => {
    if (a.status === 'in_library') parts.push(`<a class="btn btn-secondary" href="${escapeAttr(p.url)}" target="_blank" rel="noopener">Open in ${escapeAttr(p.name)}</a>`);
  });
  if (a.status === 'needs_review' && isAdmin && a.verdict && (a.verdict.options || []).length) {
    parts.push(...a.verdict.options.map(o =>
      `<button class="btn btn-secondary" onclick="resolveMusicReview('${escapeJS(a.mbid)}', ${Number(o.tracks)}, this)">Keep ${Number(o.tracks)} tracks</button>`));
  }
  return parts.length ? `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">${parts.join('')}</div>` : '';
}

function _musicAlbumBody(a) {
  const verdictMsg = a.verdict && (a.verdict.message || a.verdict.state);
  let html = `<div style="display:flex;gap:18px;flex-wrap:wrap">
      ${_musicCover(a.cover, 160)}
      <div style="flex:1;min-width:200px">
        <div style="font-size:13px;color:var(--text2)"><a href="#" onclick="openMusicArtist('${escapeJS(a.artist_mbid)}');return false" style="color:var(--accent)">${escapeAttr(a.artist)}</a></div>
        <div style="font-size:12px;color:var(--text3);margin-top:2px">${escapeAttr([a.year, a.type].filter(Boolean).join(' · '))}</div>
        <div style="margin-top:8px">${_musicBadge(a, 'Not requested')}</div>
        ${verdictMsg ? `<div style="font-size:12px;color:var(--text2);margin-top:8px">${escapeAttr(verdictMsg)}</div>` : ''}
        ${_musicActions(a)}
      </div>
    </div>`;
  if (a.original && a.release) {
    const r = a.release;
    html += `<div class="music-original">
        <div><strong>Original:</strong> ${a.original.year} · ${a.original.tracks} tracks${a.original.discs > 1 ? ' · ' + a.original.discs + ' discs' : ''}</div>
        <div style="color:var(--text3);font-size:11px;margin-top:2px">Tracklist of ${escapeAttr(r.title)}${r.disambiguation ? ' (' + escapeAttr(r.disambiguation) + ')' : ''} · ${escapeAttr([r.format, r.country, r.date].filter(Boolean).join(', '))}</div>
      </div>${_musicTracklist(r.tracklist)}`;
  } else if (a.review) {
    html += `<div class="music-original" style="border-left-color:var(--amber)">
        <div><strong>Which tracklist is the original?</strong></div>
        <div style="color:var(--text2);font-size:12px;margin-top:4px">${escapeAttr(a.review.message)} ${(a.review.options || []).length
          ? "Tentacle won't guess: pick one."
          : 'Request it, then pick the edition under Library → Music → Needs review once Lidarr has loaded its releases.'}</div>
      </div>` + (a.review.options || []).map(o => `
      <div class="music-option">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <strong>${o.tracks} tracks</strong>
          <span style="color:var(--text3);font-size:12px">${o.releases} release${o.releases === 1 ? '' : 's'}, e.g. ${escapeAttr((o.examples || []).join('; '))}</span>
          ${a.status === 'available' ? `<button class="btn btn-secondary btn-sm" style="margin-left:auto" onclick="requestMusicAlbum('${escapeJS(a.mbid)}', this, ${Number(o.tracks)})">Request with ${o.tracks} tracks</button>` : ''}
        </div>
        <details style="margin-top:6px"><summary style="cursor:pointer;font-size:12px;color:var(--text3)">Tracklist</summary>${_musicTracklist(o.tracklist)}</details>
      </div>`).join('');
  }
  return html;
}

async function openMusicAlbum(mbid) {
  const seq = ++musicState.seq;
  _musicLoading('Loading…');
  try {
    const a = await api(`/api/music/album/${encodeURIComponent(mbid)}`);
    if (seq !== musicState.seq) return;
    _musicModal(a.title || 'Album', _musicAlbumBody(a));
  } catch (e) {
    if (seq === musicState.seq) _musicModal('Album', `<div class="empty-state"><p>${escapeAttr(e.message)}</p></div>`);
  } finally {
    musicState.highlight = null;
  }
}

async function openMusicArtist(mbid) {
  const seq = ++musicState.seq;
  _musicLoading('Loading…');
  try {
    const a = await api(`/api/music/artist/${encodeURIComponent(mbid)}`);
    if (seq !== musicState.seq) return;
    const meta = [a.type, a.country, a.years, a.disambiguation].filter(Boolean).join(' · ');
    const groups = {};
    (a.other || []).forEach(c => { (groups[c.type] = groups[c.type] || []).push(c); });
    const other = Object.keys(groups).sort().map(t =>
      `<div class="music-subhead">${escapeAttr(t)}</div><div class="music-grid">${groups[t].map(c => _musicAlbumCard(c, { showArtist: false })).join('')}</div>`).join('');
    _musicModal(a.name || 'Artist', `
      <div style="font-size:12px;color:var(--text3);margin-bottom:12px">${escapeAttr(meta)}${a.in_lidarr ? ' <span class="badge badge-green" style="font-size:9px;padding:1px 5px">In Lidarr</span>' : ''}</div>
      <div class="music-subhead">Studio albums</div>
      ${(a.studio || []).length ? `<div class="music-grid">${a.studio.map(c => _musicAlbumCard(c, { showArtist: false })).join('')}</div>`
        : '<div style="color:var(--text3);font-size:12px">MusicBrainz lists no studio albums.</div>'}
      ${(a.other || []).length ? `<details style="margin-top:16px"><summary style="cursor:pointer;font-size:13px;color:var(--text2)">Show all releases: live, compilations, EPs, singles (${a.other.length})</summary>${other}</details>` : ''}`);
  } catch (e) {
    if (seq === musicState.seq) _musicModal('Artist', `<div class="empty-state"><p>${escapeAttr(e.message)}</p></div>`);
  }
}

async function openMusicSong(title, artistMbid) {
  const seq = ++musicState.seq;
  _musicLoading(title);
  try {
    const s = await api(`/api/music/song?title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artistMbid)}`);
    if (seq !== musicState.seq) return;
    if (s.album) {
      musicState.highlight = s.highlight;
      const a = s.album;
      _musicModal(a.title || title, `<div class="music-original" style="margin-top:0;margin-bottom:14px">“${escapeAttr(title)}” first appeared on this studio album.</div>` + _musicAlbumBody(a));
      musicState.highlight = null;
    } else {
      _musicModal(title, `<div class="empty-state"><p>${escapeAttr(s.message)}</p></div>`);
    }
  } catch (e) {
    if (seq === musicState.seq) _musicModal(title, `<div class="empty-state"><p>${escapeAttr(e.message)}</p></div>`);
  }
}

async function requestMusicAlbum(mbid, btn, tracks) {
  if (btn) { btn.disabled = true; btn.dataset.label = btn.textContent; btn.textContent = btn.classList.contains('lib-card-add-btn') ? '…' : 'Requesting…'; }
  try {
    const body = { mbid };
    if (tracks) body.tracks = tracks;
    const r = await api('/api/music/request', { method: 'POST', body });
    toast(`Requested ${escapeAttr(r.title || 'the album')} — Tentacle pins the original release, then Lidarr searches`, 'success', 6000);
    if (btn && btn.classList.contains('lib-card-add-btn')) btn.remove();
    musicState.listCache = {};
    if (_musicDiscoverVisible()) setTimeout(_loadMusicDiscover, 1500);   // the badge turns "Wanted"
    if (document.getElementById('modal-music').style.display === 'flex' && btn && !btn.classList.contains('lib-card-add-btn')) openMusicAlbum(mbid);
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label || 'Request'; }
  }
}

async function resolveMusicReview(mbid, tracks, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(`/api/music/review/${encodeURIComponent(mbid)}`, { method: 'POST', body: { tracks } });
    toast(`Keeping the ${tracks}-track tracklist — pinning it in Lidarr`, 'success');
    setTimeout(() => { if (typeof loadMusicLibrary === 'function') loadMusicLibrary(); }, 1500);
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    if (btn) btn.disabled = false;
  }
}

// ── Library: Music tab ────────────────────────────────────────────────────

function setMusicLibFilter(filter, btn) {
  musicState.libFilter = filter;
  document.querySelectorAll('[data-musicfilter]').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  loadMusicLibrary();
}

async function loadMusicLibrary() {
  const el = document.getElementById('music-library');
  if (!el) return;
  const isAdmin = state.currentUser && state.currentUser.is_admin;
  if (isAdmin) loadMusicStatusLine();
  if (musicState.libFilter === 'fix') return loadMusicFix(el);
  try {
    const data = await api(`/api/music/library?status=${encodeURIComponent(musicState.libFilter)}`);
    const c = data.counts || {};
    for (const key of ['all', 'in_library', 'downloading', 'wanted', 'needs_review']) {
      const n = document.getElementById(`music-count-${key}`);
      if (n) n.textContent = c[key] ? ` ${c[key]}` : '';
    }
    if (!data.artists.length) {
      el.innerHTML = `<div class="empty-state" style="padding:40px"><p>${musicState.libFilter === 'all'
        ? 'No monitored albums yet. Search for music in Discover → Music.' : 'Nothing here.'}</p></div>`;
      return;
    }
    el.innerHTML = data.artists.map(artist => `
      <div class="music-lib-artist">
        <div class="music-lib-artist-name"><a href="#" onclick="openMusicArtist('${escapeJS(artist.mbid)}');return false">${escapeAttr(artist.name)}</a></div>
        ${artist.albums.map(a => _musicLibraryRow(a, data.players, isAdmin)).join('')}
      </div>`).join('');
  } catch (e) {
    el.innerHTML = `<div class="empty-state" style="padding:40px"><p>${escapeAttr(e.message)}</p></div>`;
  }
}

function _musicLibraryRow(a, players, isAdmin) {
  const open = a.status === 'in_library' ? (players || []).map(p =>
    `<a class="btn btn-secondary btn-sm" href="/api/music/open/${encodeURIComponent(a.mbid)}?player=${encodeURIComponent(p.id)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Open in ${escapeAttr(p.name)}</a>`).join('') : '';
  const review = a.status === 'needs_review' && isAdmin ? (a.options || []).map(o =>
    `<button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();resolveMusicReview('${escapeJS(a.mbid)}', ${Number(o.tracks)}, this)">Keep ${Number(o.tracks)} tracks</button>`).join('') : '';
  const files = a.tracks ? `${a.files}/${a.tracks} tracks` : '';
  return `<div class="music-lib-row" onclick="openMusicAlbum('${escapeJS(a.mbid)}')">
      ${_musicCover(a.cover, 48)}
      <div class="music-row-main">
        <div class="music-row-title">${escapeAttr(a.title)} ${_musicBadge(a)}</div>
        <div class="music-row-sub">${escapeAttr([a.year, files].filter(Boolean).join(' · '))}${a.status === 'needs_review' && a.message ? ' — ' + escapeAttr(a.message) : ''}</div>
      </div>
      <div class="music-lib-actions">${open}${review}</div>
    </div>`;
}

async function loadMusicStatusLine() {
  const el = document.getElementById('music-status-summary');
  if (!el) return;
  try {
    const s = await api('/api/music/status');
    const run = s.reconcile_running;
    const last = s.last_reconcile;
    if (run) {
      el.textContent = `Checking the library… ${run.done}/${run.total || '?'} artists`;
    } else if (last) {
      el.textContent = `Last check ${new Date(last.finished).toLocaleString()}: ${last.albums_checked} albums` +
        (s.needs_review ? `, ${s.needs_review} need review` : '') + (last.errors ? `, ${last.errors} couldn't be checked` : '');
    } else {
      el.textContent = 'Not checked yet.';
    }
  } catch (_) { el.textContent = ''; }
}

async function startMusicReconcile(btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/music/reconcile', { method: 'POST' });
    toast(escapeAttr(r.message), 'info');
    loadMusicStatusLine();
  } catch (e) {
    toast(escapeAttr(e.message), 'error');
  } finally {
    if (btn) setTimeout(() => { btn.disabled = false; }, 3000);
  }
}

// ── Library: Fix library (the reconcile review page, admins) ──────────────

const _FIX_GROUPS = [
  ['repin', 'Re-pin', 'Your files already fit the original release. Tentacle pins it; Lidarr re-matches the files.'],
  ['repin_trim', 'Re-pin and remove extra tracks', 'Pins the original, then Lidarr deletes the tracks it no longer has (deluxe and bonus tracks).'],
  ['repin_download', 'Re-pin and download', 'Pins the original, then Lidarr searches for the missing tracks.'],
];

let _musicRecycleBin = null;

function _recycleNote(bin) {
  _musicRecycleBin = bin;
  if (bin === '') return ' <span style="color:var(--amber)">Lidarr has no recycle bin set, so these tracks would be deleted for good. Set one in Lidarr → Settings → Media Management first.</span>';
  if (bin) return ` They go to Lidarr's recycle bin (${escapeAttr(bin)}).`;
  return '';
}

function _deleteWarning() {
  if (_musicRecycleBin === '') return ' Lidarr has no recycle bin set: they are deleted for good.';
  if (_musicRecycleBin) return " They go to Lidarr's recycle bin.";
  return '';
}

function _releaseLine(r) {
  if (!r) return '?';
  return `${r.tracks} tracks · ${escapeAttr(r.format || 'unknown format')}${r.discs > 1 ? ' · ' + r.discs + ' discs' : ''}` +
    `${r.disambiguation ? ' · ' + escapeAttr(r.disambiguation) : ''}`;
}

function _fixRow(a, applicable, trim) {
  const trimCount = trim ? Math.max(0, (a.have || 0) - ((a.target || {}).tracks || 0)) : 0;
  const change = applicable
    ? `<div class="music-fix-change">Now: ${_releaseLine(a.pinned)} → Original: ${_releaseLine(a.target)}${a.have ? ' · you have ' + a.have + ' files' : ''}</div>`
    : `<div class="music-fix-change">${escapeAttr(a.message || '')}</div>`;
  const action = a.state
    ? '<span class="badge badge-accent" style="font-size:10px">Applying…</span>'
    : applicable
      ? `<button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();applyMusicFix(['${escapeJS(a.mbid)}'], this, ${trimCount})">Apply</button>`
      : (a.options || []).map(o => `<button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();resolveMusicReview('${escapeJS(a.mbid)}', ${Number(o.tracks)}, this)">Keep ${Number(o.tracks)} tracks</button>`).join('');
  return `<div class="music-lib-row" onclick="openMusicAlbum('${escapeJS(a.mbid)}')">
      ${_musicCover(a.cover, 44)}
      <div class="music-row-main">
        <div class="music-row-title">${escapeAttr(a.artist)} — ${escapeAttr(a.title)}${a.year ? ' (' + a.year + ')' : ''}</div>
        ${change}
      </div>
      <div class="music-lib-actions">${action}</div>
    </div>`;
}

async function loadMusicFix(el) {
  let d;
  try {
    d = await api('/api/music/review');
  } catch (e) {
    el.innerHTML = `<div class="empty-state" style="padding:40px"><p>${escapeAttr(e.message)}</p></div>`;
    return;
  }
  const g = d.groups || {};
  const total = ['repin', 'repin_trim', 'repin_download', 'review'].reduce((n, k) => n + (g[k] || []).length, 0);
  const count = document.getElementById('music-count-fix');
  if (count) count.textContent = total + (d.pictures || []).length ? ` ${total + (d.pictures || []).length}` : '';
  let html = '<p class="form-hint" style="margin:0 0 16px">Nothing changes until you press Apply, except the kinds you set to apply automatically in Settings → Music.</p>';
  for (const [key, title, desc] of _FIX_GROUPS) {
    const items = g[key] || [];
    if (!items.length) continue;
    html += `<div class="music-fix-group">
      <div class="music-fix-head">
        <span class="music-fix-title">${title} (${items.length})</span>
        ${d.auto && d.auto[key] ? '<span class="badge badge-green" style="font-size:10px">Applied automatically</span>' : ''}
        <button class="btn btn-primary btn-sm" onclick="applyMusicFixGroup('${key}', ${items.length}, this)">Apply all ${items.length}</button>
        <span class="music-fix-desc">${desc}${key === 'repin_trim' ? _recycleNote(d.recycle_bin) : ''}</span>
      </div>
      ${items.map(a => _fixRow(a, true, key === 'repin_trim')).join('')}
    </div>`;
  }
  if ((g.review || []).length) {
    html += `<div class="music-fix-group"><div class="music-fix-head"><span class="music-fix-title">Needs review (${g.review.length})</span>
      <span class="music-fix-desc">Tentacle won't guess these. Pick a tracklist; nothing downloaded is changed until you do.</span></div>
      ${g.review.map(a => _fixRow(a, false)).join('')}</div>`;
  }
  if ((d.pictures || []).length) {
    html += `<div class="music-fix-group"><div class="music-fix-head"><span class="music-fix-title">Artist pictures (${d.pictures.length})</span>
      <span class="music-fix-desc">Pick the right photo, or upload one. It is set in your player.</span></div>
      ${d.pictures.map(p => `<div class="music-lib-row" style="cursor:default;flex-wrap:wrap">
        <div class="music-row-main">
          <div class="music-row-title">${escapeAttr(p.name)}${p.disambiguation ? ' <span style="color:var(--text3)">(' + escapeAttr(p.disambiguation) + ')</span>' : ''}</div>
          <div class="music-fix-change">${escapeAttr(p.note)}</div>
          ${(p.candidates || []).length ? `<div class="music-picks">${p.candidates.filter(c => c.thumb).map(c =>
            `<button class="music-pick" title="${escapeAttr(c.name)} — ${c.albums || 0} albums on Deezer" onclick="pickMusicPicture('${escapeJS(p.mbid)}', ${Number(c.id)}, this)"><img src="${escapeAttr(c.thumb)}" loading="lazy">${escapeAttr(c.name)}</button>`).join('')}</div>` : ''}
        </div>
        <div class="music-lib-actions">
          <label class="btn btn-secondary btn-sm" style="cursor:pointer">Upload…<input type="file" accept="image/*" style="display:none" onchange="uploadMusicPicture('${escapeJS(p.mbid)}', this)"></label>
        </div>
      </div>`).join('')}</div>`;
  }
  if (d.unlocked) {
    html += `<div class="music-fix-group"><div class="music-fix-head"><span class="music-fix-title">Right, but not locked (${d.unlocked})</span>
      <button class="btn btn-secondary btn-sm" onclick="lockMusicAlbums(this)">Lock all</button>
      <span class="music-fix-desc">Pinned to the original, but Lidarr may still import another edition ("any release OK" is on). Locking turns it off; no files change.</span></div></div>`;
  }
  if (!total && !(d.pictures || []).length && !d.unlocked) {
    html = '<div class="empty-state" style="padding:40px"><p>Nothing to fix: every checked album is pinned to its original.</p></div>';
  }
  el.innerHTML = html;
}

async function applyMusicFix(mbids, btn, trimCount) {
  if (trimCount && !confirm(`Re-pin and have Lidarr delete ${trimCount} extra track${trimCount === 1 ? '' : 's'}?${_deleteWarning()}`)) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/music/apply', { method: 'POST', body: { mbids } });
    toast(`Applying ${r.queued} album${r.queued === 1 ? '' : 's'}…`, 'info');
    setTimeout(loadMusicLibrary, 1200);
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    if (btn) btn.disabled = false;
  }
}

async function applyMusicFixGroup(category, n, btn) {
  const note = category === 'repin_trim' ? ' Lidarr will delete the extra tracks.' + _deleteWarning() : '';
  if (!confirm(`Apply to all ${n} albums?${note}`)) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/music/apply', { method: 'POST', body: { category } });
    toast(`Applying ${r.queued} albums, one at a time…`, 'info', 6000);
    setTimeout(loadMusicLibrary, 1500);
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    if (btn) btn.disabled = false;
  }
}

async function lockMusicAlbums(btn) {
  if (btn) btn.disabled = true;
  try {
    await api('/api/music/lock', { method: 'POST' });
    toast('Locking albums to their original release…', 'info');
    setTimeout(loadMusicLibrary, 3000);
  } catch (e) {
    toast(escapeAttr(e.message), 'error');
    if (btn) btn.disabled = false;
  }
}

async function pickMusicPicture(mbid, deezerId, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(`/api/music/artist/${encodeURIComponent(mbid)}/picture/deezer`, { method: 'POST', body: { deezer_id: deezerId } });
    toast('Setting the picture…', 'info');
    setTimeout(loadMusicLibrary, 2500);
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
    if (btn) btn.disabled = false;
  }
}

async function uploadMusicPicture(mbid, input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const r = await fetch(`/api/music/artist/${encodeURIComponent(mbid)}/picture`, { method: 'POST', body: form });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || 'Upload failed');
    }
    toast('Setting the picture…', 'info');
    setTimeout(loadMusicLibrary, 2500);
  } catch (e) {
    toast(escapeAttr(e.message), 'error', 8000);
  }
}
