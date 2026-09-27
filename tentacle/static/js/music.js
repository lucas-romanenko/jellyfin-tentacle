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
  const sub = opts.showArtist === false ? (a.year || '—') : `${escapeAttr(a.artist || '')}${a.year ? ' · ' + a.year : ''}`;
  return `<div class="lib-card music" onclick="openMusicAlbum('${escapeJS(a.mbid)}')">
      <div class="lib-card-poster">${cover}<div class="lib-card-source">${_musicBadge(a)}</div>${addBtn}</div>
      <div class="lib-card-info">
        <div class="lib-card-title" title="${escapeAttr(a.title)}">${escapeAttr(a.title)}</div>
        <div class="lib-card-meta">${sub}</div>
      </div>
    </div>`;
}

// ── Discover: search ──────────────────────────────────────────────────────

function musicDiscoverHome() {
  const grid = document.getElementById('discover-grid');
  const tabs = document.getElementById('discover-section-tabs');
  const pills = document.getElementById('discover-streaming-pills');
  if (tabs) tabs.innerHTML = '';
  if (pills) pills.style.display = 'none';
  grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:40px">
      <p>Search for an artist, an album or a song.</p>
      <p style="font-size:12px;color:var(--text3)">Tentacle finds the original studio album and asks Lidarr for it, with its original tracklist.</p>
    </div>`;
}

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
