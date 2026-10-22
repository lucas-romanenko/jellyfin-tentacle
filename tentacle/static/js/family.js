// Tentacle - Family (Roadmap 1c): the admin's People and Requests pages, the
// invite and "Sign in a TV" dialogs, and the plain-words banner for family.
//
// People: one row per account with its age limit, how its requests work and
// the TV app it runs. Names come from Jellyfin and are text, never markup
// (every value goes through escHtml; rows carry only numeric ids).
// Requests: what family asked for, answered with Approve or Decline.

const fam = {
  data: null,          // GET /api/family/people
  requests: null,      // GET /api/requests
  reqTab: 'pending',
  invited: null,       // the last invite: {user, invite}
  tvFor: null,         // person id the TV code is for (null = me)
  kids: null,          // GET /api/family/kids-channels
  kidsChosen: new Set(),
  open: new Set(),     // person ids whose "What they can do" panel is open
  server: null,        // GET /api/family/server (Settings → People & sign-in)
};

const FAM_LEVEL_NAMES = { young: 'Young kids', kids: 'Kids', teens: 'Teens' };

function _famAgo(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (!t) return '';
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 2) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const h = Math.round(mins / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return d === 1 ? 'yesterday' : d < 30 ? `${d} days ago` : new Date(iso).toLocaleDateString();
}

// ── After sign-in: request mode for everyone, banner, the admins' badge ──

async function famInit() {
  try {
    const me = await api('/api/family/me');
    state.requestMode = { mode: me.mode, approver: me.approver };
    // A kids profile has no Discover (TMDB's whole catalogue, any rating).
    state.kidsProfile = !!me.kids;
    const disc = document.querySelector('.nav-item[data-page="discover"]');
    if (disc) disc.style.display = me.kids ? 'none' : '';
    if (me.kids && state.currentPage === 'discover') showPage('library');
    famRenderBanner(me.messages || []);
  } catch (e) {
    state.requestMode = null;
  }
  if (state.currentUser && state.currentUser.is_admin) famLoadBadge();
  if (!state._famTimer) {
    state._famTimer = setInterval(() => {
      if (state.currentUser && state.currentUser.is_admin) famLoadBadge();
      else famRefreshBanner();
    }, 120000);
  }
}

function stopFamilyPolling() {
  if (state._famTimer) { clearInterval(state._famTimer); state._famTimer = null; }
}

async function famRefreshBanner() {
  try { famRenderBanner((await api('/api/family/status')).messages || []); } catch (e) { /* quiet */ }
}

// Family see one plain line; admins have Health for the details.
function famRenderBanner(messages) {
  const el = document.getElementById('family-banner');
  if (!el) return;
  const show = messages.length && !(state.currentUser && state.currentUser.is_admin);
  el.style.display = show ? '' : 'none';
  el.innerHTML = show ? messages.map(m => `<div>${escHtml(m.text)}</div>`).join('') : '';
}

async function famLoadBadge() {
  const b = document.getElementById('requests-badge');
  if (!b) return;
  try {
    const n = (await api('/api/requests/count')).pending || 0;
    b.textContent = n;
    b.style.display = n ? '' : 'none';
  } catch (e) { /* keep the last count */ }
}

// ── People ───────────────────────────────────────────────────────────────

async function loadPeople() {
  const el = document.getElementById('fam-people');
  try {
    fam.data = await api('/api/family/people');
  } catch (e) {
    el.innerHTML = `<div class="fam-error">Couldn't load your household: ${escHtml(e.message)}</div>`;
    return;
  }
  fam.seen = JSON.stringify(fam.data);
  famRenderPeople();
  _famWatch();
}

// Live while People is open: someone locked out, added or changed by another
// admin shows within seconds. Never while a dialog is open or a control on
// the page has the focus (redrawing would close a list being picked from).
function _famWatch() {
  if (fam.watch) return;
  fam.watch = setInterval(async () => {
    if (state.currentPage !== 'people') { clearInterval(fam.watch); fam.watch = null; return; }
    const busy = document.hidden || document.querySelector('.modal-overlay[style*="flex"]')
      || document.activeElement?.closest?.('#page-people select, #page-people input');
    if (busy || fam.fetching) return;
    fam.fetching = true;
    try {
      const d = await api('/api/family/people');
      const sig = JSON.stringify(d);
      if (sig !== fam.seen && state.currentPage === 'people') { fam.data = d; fam.seen = sig; famRenderPeople(); }
    } catch (e) { /* the next round tries again */ }
    fam.fetching = false;
  }, 8000);
}

function _famAvatar(p) {
  const jf = state.currentUser?.jellyfin_url || '';
  if (p.image_tag && jf) {
    const src = `${jf}/Users/${encodeURIComponent(p.jellyfin_user_id)}/Images/Primary?tag=${encodeURIComponent(p.image_tag)}&quality=90&maxWidth=80`;
    return `<img class="fam-avatar" alt="" src="${escHtml(src)}">`;
  }
  return `<div class="fam-avatar">${escHtml((p.name || '?').charAt(0).toUpperCase())}</div>`;
}

function _famTv(p, newest) {
  const v = p.tv_app && p.tv_app.version;
  if (!v) return '<span class="fam-meta-item">No Tentacle TV yet</span>';
  if (p.tv_app.up_to_date || !newest) return `<span class="fam-meta-item">TV app ${escHtml(v)} &#10003;</span>`;
  return `<span class="fam-meta-item fam-warn-text" title="The TV offers the new version the next time Tentacle opens">TV app ${escHtml(v)} &middot; ${escHtml(newest)} waiting</span>`;
}

function famRenderPeople() {
  const d = fam.data;
  const el = document.getElementById('fam-people');
  document.getElementById('fam-qc-warning').style.display = d.quick_connect === false ? '' : 'none';
  const me = state.currentUser?.jellyfin_user_id;
  const levels = d.kids_levels || [];
  el.innerHTML = d.people.map(p => {
    const chips = [];
    if (p.is_owner) chips.push('<span class="badge badge-accent">Owner</span>');
    else if (p.is_admin) chips.push('<span class="badge badge-green">Admin</span>');
    if (p.kids_level) chips.push(`<span class="badge badge-pink">${escHtml(FAM_LEVEL_NAMES[p.kids_level] || 'Kid')}</span>`);
    if (p.locked) chips.push('<span class="badge badge-red" title="Jellyfin locks an account after 5 wrong passwords in a row">Locked: wrong passwords</span>');
    else if (p.disabled) chips.push('<span class="badge badge-gray">Turned off</span>');
    chips.push(..._famLimitChips(p));
    const meta = [_famTv(p, d.tv_app && d.tv_app.newest)];
    if (p.last_active) meta.push(`<span class="fam-meta-item">Active ${escHtml(_famAgo(p.last_active))}</span>`);
    if (p.invited_by) meta.push(`<span class="fam-meta-item">Invited by ${escHtml(p.invited_by)}</span>`);
    const kidsSel = p.is_admin
      ? '<span class="fam-fixed">No limit</span>'
      : `<select class="form-input fam-select" aria-label="Age limit for ${escHtml(p.name)}" onchange="famSetKids(${p.id}, this.value)">
          <option value=""${p.kids_level ? '' : ' selected'}>No limit</option>
          ${levels.map(l => `<option value="${escHtml(l.id)}"${p.kids_level === l.id ? ' selected' : ''}>${escHtml(l.name)} (${escHtml(l.allows)})</option>`).join('')}
        </select>`;
    const reqSel = p.is_admin
      ? '<span class="fam-fixed">Straight away</span>'
      : p.kids_level
        ? '<span class="fam-fixed">Ask me first</span>'
        : `<select class="form-input fam-select" aria-label="Requests from ${escHtml(p.name)}" onchange="famSetRequests(${p.id}, this.value)">
            <option value="ask"${p.request_mode === 'ask' ? ' selected' : ''}>Ask me first</option>
            <option value="direct"${p.request_mode === 'direct' ? ' selected' : ''}>Straight away</option>
          </select>`;
    const places = _famPlaceOptions(p.location ? p.location.slug : '');
    const placeSel = places && `<select class="form-input fam-select" aria-label="Where ${escHtml(p.name)} watches Live TV from" onchange="famSetLocation(${p.id}, this.value)">${places}</select>`;
    const adminLocked = p.is_owner || p.jellyfin_user_id === me || p.kids_level;
    const adminTitle = p.is_owner ? 'The owner stays an admin' : p.jellyfin_user_id === me ? 'You can’t change your own' : p.kids_level ? 'A kid can’t be an admin' : '';
    return `<div class="fam-person">
      <div class="fam-who">
        ${_famAvatar(p)}
        <div class="fam-who-text">
          <div class="fam-name">${escHtml(p.name)} ${chips.join(' ')}</div>
          <div class="fam-meta">${meta.join('')}</div>
        </div>
      </div>
      <div class="fam-controls">
        <label class="fam-ctl"><span class="fam-ctl-label">Age limit</span>${kidsSel}</label>
        <label class="fam-ctl"><span class="fam-ctl-label">Their requests</span>${reqSel}</label>
        ${placeSel ? `<label class="fam-ctl"><span class="fam-ctl-label">Live TV from</span>${placeSel}</label>` : ''}
        <label class="fam-ctl fam-ctl-admin" title="${escHtml(adminTitle)}"><span class="fam-ctl-label">Admin</span>
          <input type="checkbox"${p.is_admin ? ' checked' : ''}${adminLocked ? ' disabled' : ''} onchange="famToggleAdmin(${p.id}, this.checked)"></label>
        <label class="fam-ctl fam-ctl-admin" title="Shown on the sign-in screen at home, to pick instead of typing a name"><span class="fam-ctl-label">On sign-in screen</span>
          <input type="checkbox"${p.on_sign_in ? ' checked' : ''} onchange="famSetOnSignIn(${p.id}, this.checked)"></label>
        <div class="fam-actions">
          ${p.locked && !p.is_admin
            ? `<button class="btn btn-primary btn-sm" onclick="famUnlock(${p.id})">Unlock</button>`
            : p.disabled && !p.is_admin
            ? `<button class="btn btn-primary btn-sm" onclick="famSetAccess(${p.id}, {disabled: false})">Turn on</button>`
            : `<button class="btn btn-secondary btn-sm" onclick="famShowTvCode(${p.id})">Sign in a TV</button>`}
          ${p.is_admin ? '' : `<button class="btn btn-secondary btn-sm" onclick="famNewPassword(${p.id})">New password</button>`}
          ${p.is_admin || !p.access ? '' : `<button class="btn btn-secondary btn-sm fam-more" aria-expanded="${fam.open.has(p.id)}" aria-controls="fam-access-${p.id}" onclick="famToggleAccess(${p.id})">More <span aria-hidden="true">${fam.open.has(p.id) ? '&#9652;' : '&#9662;'}</span></button>`}
        </div>
      </div>
      ${p.is_admin || !p.access || !fam.open.has(p.id) ? '' : _famAccessPanel(p)}
    </div>`;
  }).join('') || '<div class="empty-state"><p>Nobody yet.</p></div>';

  // New people start with
  const home = document.getElementById('fam-default-home');
  const chan = document.getElementById('fam-default-channels');
  const named = d.people.filter(p => !p.kids_level);
  home.innerHTML = '<option value="">The default home (My Home Screen)</option>' +
    named.map(p => `<option value="${p.id}"${d.defaults.home_from === p.id ? ' selected' : ''}>The same as ${escHtml(p.name)}</option>`).join('');
  const lists = named.filter(p => p.favourite_count || d.defaults.channels_from === p.id);
  const nfav = n => `${n} favourite${n === 1 ? '' : 's'}`;
  chan.innerHTML = '<option value="">No favourites yet (they star their own)</option>' +
    lists.map(p => `<option value="${p.id}"${d.defaults.channels_from === p.id ? ' selected' : ''}>${escHtml(p.name)}’s favourites (${nfav(p.favourite_count || 0)})</option>`).join('');
  const kd = d.defaults.kids || { home: 'kids', channels: 'kids' };
  const kidsHome = document.getElementById('fam-kids-home');
  if (kidsHome) {
    kidsHome.innerHTML = '<option value="kids">Ready-made kids home</option>' +
      '<option value="default">The default home (My Home Screen)</option>' +
      d.people.filter(p => p.kids_level).map(p => `<option value="${p.id}">The same as ${escHtml(p.name)}</option>`).join('');
    kidsHome.value = String(kd.home);
    document.getElementById('fam-kids-channels').value = kd.channels;
  }
  const names = d.kids_channel_names || [];
  document.getElementById('fam-kids-summary').textContent = d.kids_channels
    ? `Kids see ${d.kids_channels === 1 ? 'one channel' : d.kids_channels + ' channels'} in Live TV: ${names.join(', ')}${d.kids_channels > names.length ? ' and more' : ''}.`
    : 'None picked yet, so kids have no Live TV.';
  document.getElementById('fam-tv-address').textContent = d.addresses.tv_app || 'Set Tentacle’s address in Settings → Services first';
  document.getElementById('fam-tv-newest').textContent = d.tv_app && d.tv_app.newest ? `Newest version: ${d.tv_app.newest}.` : '';
}

function _famPerson(id) { return (fam.data?.people || []).find(p => p.id === id); }

// ── What each person can do (Jellyfin's own settings, read from Jellyfin) ──

function _famTime(h) {
  const d = new Date(2000, 0, 1, Math.floor(h % 24), Math.round((h % 1) * 60));
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function _famHoursText(hours) {
  if (!hours) return 'Any time';
  if (hours.custom) return 'Set in Jellyfin';
  return `${_famTime(hours.start)} to ${_famTime(hours.end)}`;
}

// Chips for what differs from a new person's defaults, so the list reads at a glance.
function _famLimitChips(p) {
  const a = p.access;
  if (!a || p.is_admin) return [];
  const out = [];
  if (!a.away) out.push('<span class="badge badge-gray">Home only</span>');
  if (a.libraries) out.push(`<span class="badge badge-gray">${a.libraries.length === 1 ? '1 library' : a.libraries.length + ' libraries'}</span>`);
  if (a.hours) out.push(`<span class="badge badge-gray">${escHtml(_famHoursText(a.hours))}</span>`);
  return out;
}

function _famHourOptions(sel) {
  sel = sel % 24;   // 24:00 (Jellyfin's end of day) is midnight
  let o = '';
  for (let h = 0; h < 24; h += 0.5) o += `<option value="${h}"${h === sel ? ' selected' : ''}>${escHtml(_famTime(h))}</option>`;
  return o;
}

function _famAccessPanel(p) {
  const a = p.access;
  const libs = fam.data.libraries || [];
  const kid = !!p.kids_level;
  const some = !!a.libraries;
  const chosen = new Set(a.libraries || []);
  const hours = a.hours && !a.hours.custom ? a.hours : null;
  const libList = some ? `<div class="fam-libs">${libs.map(l => `
      <label class="fam-check"><input type="checkbox" data-lib="${escHtml(l.id)}"${chosen.has(l.id) ? ' checked' : ''}
        onchange="famSetLibrary(${p.id})"> ${escHtml(l.name)}</label>`).join('')}</div>` : '';
  return `<div class="fam-access" id="fam-access-${p.id}">
    <div class="fam-access-item">
      <label class="fam-check"><input type="checkbox"${a.can_record ? ' checked' : ''}${kid ? ' disabled' : ''}
        onchange="famSetAccess(${p.id}, {can_record: this.checked})"> <strong>Can record shows</strong></label>
      <div class="fam-hint">${kid ? 'Kids can’t record.' : 'Schedule and cancel recordings in Live TV. Recordings are shared by everyone.'}</div>
    </div>
    <div class="fam-access-item">
      <label class="fam-check"><input type="checkbox"${p.live_tv ? ' checked' : ''}
        onchange="famSetAccess(${p.id}, {live_tv: this.checked})"> <strong>Live TV</strong></label>
      <div class="fam-hint">${kid ? 'Only the kids channels you picked.' : 'The guide and every channel; they star their favourites.'}</div>
    </div>
    <div class="fam-access-item">
      <label class="fam-check"><input type="checkbox"${a.away ? ' checked' : ''}
        onchange="famSetAccess(${p.id}, {away: this.checked})"> <strong>Can watch away from home</strong></label>
      <div class="fam-hint">Off: only on your home network.</div>
    </div>
    <div class="fam-access-item">
      <div class="fam-ctl-label">Libraries</div>
      <select class="form-input fam-select" aria-label="Libraries ${escHtml(p.name)} sees" onchange="famSetLibraries(${p.id}, this.value)">
        <option value="all"${some ? '' : ' selected'}>All libraries</option>
        <option value="some"${some ? ' selected' : ''}>Only some</option>
      </select>
      ${libList}
      <div class="fam-hint">${some ? 'New libraries stay hidden from them until you tick them.' : 'Including any you add later.'}</div>
    </div>
    <div class="fam-access-item">
      <div class="fam-ctl-label">Watching hours</div>
      <select class="form-input fam-select" aria-label="When ${escHtml(p.name)} can watch" onchange="famSetHoursMode(${p.id}, this.value)">
        <option value="any"${a.hours ? '' : ' selected'}>Any time</option>
        <option value="range"${hours ? ' selected' : ''}>Only between</option>
        ${a.hours && a.hours.custom ? '<option value="custom" selected>Set in Jellyfin</option>' : ''}
      </select>
      ${hours ? `<div class="fam-hours">
        <select class="form-input" aria-label="From" id="fam-from-${p.id}" onchange="famSetHours(${p.id})">${_famHourOptions(hours.start)}</select>
        <span>and</span>
        <select class="form-input" aria-label="Until" id="fam-to-${p.id}" onchange="famSetHours(${p.id})">${_famHourOptions(hours.end)}</select>
      </div>` : ''}
      <div class="fam-hint">${hours ? 'Every day, by the server’s clock. Outside these hours they can’t sign in or play.' : 'Like a bedtime: outside the hours they can’t sign in or play.'}</div>
    </div>
    <div class="fam-access-item fam-access-off">
      ${p.locked
        ? `<button class="btn btn-primary btn-sm" onclick="famUnlock(${p.id})">Unlock ${escHtml(p.name)}</button>
           <div class="fam-hint">Locked after 5 wrong passwords in a row. Unlocking lets them sign in again.</div>`
        : p.disabled
        ? `<button class="btn btn-primary btn-sm" onclick="famSetAccess(${p.id}, {disabled: false})">Turn ${escHtml(p.name)}’s account on</button>
           <div class="fam-hint">Turned off: they can’t sign in or watch.</div>`
        : `<button class="btn btn-secondary btn-sm fam-danger" onclick="famTurnOff(${p.id})">Turn account off</button>
           <div class="fam-hint">They can’t sign in or watch until you turn it on. Nothing is deleted.</div>`}
    </div>
  </div>`;
}

function famToggleAccess(id) {
  if (fam.open.has(id)) fam.open.delete(id); else fam.open.add(id);
  famRenderPeople();
}

// Every change is answered with the person as Jellyfin has them afterwards.
async function famSetAccess(id, change, done) {
  const p = _famPerson(id);
  try {
    const fresh = await api(`/api/family/people/${id}/access`, { method: 'PUT', body: change });
    if (p) Object.assign(p, { access: fresh.access, disabled: fresh.disabled, locked: fresh.locked, live_tv: fresh.live_tv });
    toast(done || _famAccessToast(p ? p.name : 'They', change));
  } catch (e) {
    toast(e.message, 'error', 6000);
  }
  famRenderPeople();
}

function _famAccessToast(name, c) {
  if ('live_tv' in c) return c.live_tv ? `${name} has Live TV` : `${name} has no Live TV now`;
  if ('can_record' in c) return c.can_record ? `${name} can record shows` : `${name} can’t record shows now`;
  if ('away' in c) return c.away ? `${name} can watch away from home` : `${name} can watch only at home now`;
  if ('disabled' in c) return c.disabled ? `${name}’s account is off` : `${name}’s account is on again`;
  if ('libraries' in c) return c.libraries ? `${name} sees ${c.libraries.length === 1 ? 'one library' : c.libraries.length + ' libraries'}` : `${name} sees every library`;
  if ('hours' in c) return c.hours ? `${name} can watch ${_famHoursText(c.hours).replace(' to ', ' until ')}` : `${name} can watch any time`;
  return 'Saved';
}

function famSetLibraries(id, mode) {
  const libs = fam.data.libraries || [];
  if (mode === 'all') return famSetAccess(id, { libraries: null });
  if (!libs.length) { toast('Jellyfin didn’t list its libraries. Try again in a moment.', 'error'); famRenderPeople(); return; }
  // Start from everything ticked; untick what they shouldn't see.
  famSetAccess(id, { libraries: libs.map(l => l.id) }, 'Untick the libraries they shouldn’t see');
}

function famSetLibrary(id) {
  const boxes = [...document.querySelectorAll(`#fam-access-${id} input[data-lib]`)];
  const ids = boxes.filter(b => b.checked).map(b => b.dataset.lib);
  if (!ids.length) {
    toast('Leave at least one library, or turn the account off instead', 'warning');
    famRenderPeople();
    return;
  }
  famSetAccess(id, { libraries: ids });
}

function famSetHoursMode(id, mode) {
  if (mode === 'any') return famSetAccess(id, { hours: null });
  if (mode === 'range') return famSetAccess(id, { hours: { start: 7, end: 20 } });
  famRenderPeople();
}

function famSetHours(id) {
  const start = parseFloat(document.getElementById(`fam-from-${id}`).value);
  const end = parseFloat(document.getElementById(`fam-to-${id}`).value);
  if (start === end) { toast('Pick two different times', 'warning'); return; }
  famSetAccess(id, { hours: { start, end } });
}

function famUnlock(id) {
  const p = _famPerson(id);
  famSetAccess(id, { disabled: false }, `${p ? p.name : 'They'} can sign in again`);
}

function famTurnOff(id) {
  const p = _famPerson(id);
  if (!p || !confirm(`Turn ${p.name}’s account off? They can’t sign in or watch until you turn it on again. Nothing is deleted.`)) return;
  famSetAccess(id, { disabled: true });
}

// ── Settings → People & sign-in (server-wide, read from Jellyfin) ────────

async function famLoadServer() {
  const el = document.getElementById('signin-settings');
  if (!el) return;
  try {
    fam.server = await api('/api/family/server');
  } catch (e) {
    el.innerHTML = `<div class="fam-error">Couldn’t read Jellyfin’s settings: ${escHtml(e.message)}</div>`;
    return;
  }
  famRenderServer();
}

function famRenderServer() {
  const d = fam.server;
  const el = document.getElementById('signin-settings');
  const known = d.choices.some(c => c.bps === d.away_quality);
  el.innerHTML = `
    <div class="form-group">
      <label class="fam-check"><input type="checkbox" id="signin-qc"${d.quick_connect ? ' checked' : ''}
        onchange="famSaveServer({quick_connect: this.checked})"> <strong>TV sign-in codes</strong></label>
      <div class="form-hint">A TV shows a 6-digit code; type it on People (or under Sign in a TV) instead of a password on the remote. Recommended on.</div>
    </div>
    <div class="form-group">
      <div class="form-label">Quality away from home</div>
      <select class="form-input" id="signin-away" style="max-width:260px" onchange="famSaveServer({away_quality: parseInt(this.value, 10)})">
        ${known ? '' : `<option value="${d.away_quality}" selected>${(d.away_quality / 1e6).toFixed(1)} Mbps (set in Jellyfin)</option>`}
        ${d.choices.map(c => `<option value="${c.bps}"${c.bps === d.away_quality ? ' selected' : ''}>${escHtml(c.name)}</option>`).join('')}
      </select>
      <div class="form-hint">For everyone watching outside your home network. Lower it if your home internet’s upload is slow and videos stall when family watch away. At home it doesn’t apply.</div>
    </div>
    <p class="fam-hint">What each person can do (record, watch away from home, libraries, watching hours) is on <a href="#people" class="fam-link" onclick="showPage('people');return false">People</a>, under More next to each person.</p>`;
}

async function famSaveServer(change) {
  try {
    fam.server = await api('/api/family/server', { method: 'PUT', body: change });
    toast('quick_connect' in change
      ? (change.quick_connect ? 'TV sign-in codes are on' : 'TV sign-in codes are off')
      : 'Saved: quality away from home');
  } catch (e) {
    toast(e.message, 'error');
  }
  if (fam.server) famRenderServer();
}

// Where someone watches Live TV from: the places the provider's channels
// name, by country ("" when there are none, e.g. a server outside North America).
function _famPlaceOptions(current) {
  const byCountry = {};
  (fam.data?.locations || []).forEach(l => { (byCountry[l.country_name] = byCountry[l.country_name] || []).push(l); });
  return Object.entries(byCountry).map(([country, ls]) =>
    `<optgroup label="${escHtml(country)}">${ls.map(l =>
      `<option value="${escHtml(l.slug)}"${l.slug === current ? ' selected' : ''}>${escHtml(l.label)}</option>`).join('')}</optgroup>`).join('');
}

function famSetLocation(id, slug) {
  const p = _famPerson(id);
  const place = (fam.data?.locations || []).find(l => l.slug === slug);
  const where = place ? place.city : 'there';
  _famUpdate(id, { location: slug },
    p && p.is_owner ? `Live TV is from ${where} now for you and everyone without their own place`
      : `${p ? p.name : 'They'} get${p ? 's' : ''} ${where}’s local channels now`);
}

async function _famUpdate(id, body, done) {
  try {
    await api(`/api/family/people/${id}`, { method: 'PUT', body });
    toast(done);
  } catch (e) {
    toast(e.message, 'error', 6000);
  }
  loadPeople();
}

function famSetKids(id, level) {
  const p = _famPerson(id);
  const name = p ? p.name : 'They';
  _famUpdate(id, { set_kids: true, kids_level: level || null },
    level ? `${name} now has the ${FAM_LEVEL_NAMES[level]} age limit` : `${name} has no age limit now`);
}

function famSetRequests(id, mode) {
  const p = _famPerson(id);
  _famUpdate(id, { request_mode: mode },
    mode === 'ask' ? `You'll approve ${p ? p.name : 'their'}'s requests first` : `${p ? p.name : 'Their'}'s requests download straight away`);
}

function famSetOnSignIn(id, shown) {
  const p = _famPerson(id);
  const name = p ? p.name : 'They';
  _famUpdate(id, { on_sign_in: shown },
    shown ? `${name} is on the sign-in screen` : `${name} types their name to sign in now`);
}

async function famToggleAdmin(id, isAdmin) {
  const p = _famPerson(id);
  if (!p) return;
  try {
    await api('/api/auth/set-admin', { method: 'POST', body: { jellyfin_user_id: p.jellyfin_user_id, is_admin: isAdmin } });
    toast(isAdmin ? `${p.name} is an admin now` : `${p.name} isn't an admin any more`);
  } catch (e) {
    toast('Couldn’t change it: ' + e.message, 'error');
  }
  loadPeople();
}

async function famSaveDefaults() {
  const v = id => { const x = document.getElementById(id).value; return x ? parseInt(x, 10) : null; };
  try {
    const kidsHome = document.getElementById('fam-kids-home').value || 'kids';
    fam.data.defaults = await api('/api/family/defaults', { method: 'PUT',
      body: { home_from: v('fam-default-home'), channels_from: v('fam-default-channels'),
        kids: { home: /^\d+$/.test(kidsHome) ? parseInt(kidsHome, 10) : kidsHome,
          channels: document.getElementById('fam-kids-channels').value } } });
    toast('Saved: new people start like that');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function famTurnOnCodes() {
  try {
    await api('/api/family/quick-connect', { method: 'POST' });
    toast('TV sign-in codes are on');
  } catch (e) {
    toast(e.message, 'error');
  }
  loadPeople();
}

async function famCopy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied');
  } catch (e) {
    toast('Couldn’t copy: select the text and copy it', 'warning');
  }
}

// ── Add someone (Roadmap 1e): who · what they get · the message ─────────

// The password Tentacle suggests: their name in lowercase, five random
// digits and "!" (Cam → cam12457!), the shape services/family.py makes.
function famSuggestPassword(name) {
  const base = String(name || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .replace(/[^a-z0-9]/g, '').slice(0, 16) || 'tv';
  const n = new Uint32Array(1);
  crypto.getRandomValues(n);
  return `${base}${String(n[0] % 100000).padStart(5, '0')}!`;
}

function _famIsKid() {
  return document.querySelector('input[name="invite-who"]:checked').value === 'kid';
}

function famNameTyped() {
  if (fam.pwEdited) return;
  const name = document.getElementById('invite-name').value.trim();
  document.getElementById('invite-password').value = name ? famSuggestPassword(name) : '';
}

function famNewSuggestion() {
  fam.pwEdited = false;
  const name = document.getElementById('invite-name').value.trim();
  document.getElementById('invite-password').value = famSuggestPassword(name);
}

function famWhoChanged() {
  document.getElementById('invite-kids-group').style.display = _famIsKid() ? '' : 'none';
}

function famAddStep(n) {
  document.getElementById('invite-step-1').style.display = n === 1 ? '' : 'none';
  document.getElementById('invite-step-2').style.display = n === 2 ? '' : 'none';
  document.getElementById('invite-step-card').style.display = n === 3 ? '' : 'none';
  document.querySelectorAll('#invite-steps li').forEach(li => {
    const k = parseInt(li.dataset.step, 10);
    li.classList.toggle('active', k === n);
    li.classList.toggle('done', k < n);
  });
  if (n === 1) setTimeout(() => document.getElementById('invite-name').focus(), 50);
}

function famShowInvite() {
  fam.pwEdited = false;
  fam.invited = null;
  document.getElementById('invite-title').textContent = 'Add someone';
  document.getElementById('invite-steps').style.display = '';
  document.getElementById('invite-name').value = '';
  document.getElementById('invite-password').value = '';
  document.querySelector('input[name="invite-who"][value="adult"]').checked = true;
  const levels = fam.data?.kids_levels || [];
  const sel = document.getElementById('invite-kids');
  sel.innerHTML = levels.map(l => `<option value="${escHtml(l.id)}"${l.id === 'kids' ? ' selected' : ''}>${escHtml(l.name)} (${escHtml(l.allows)})</option>`).join('');
  famWhoChanged();
  const home = fam.data?.household_location;
  const places = _famPlaceOptions(home ? home.slug : '');
  document.getElementById('invite-location').innerHTML = places;
  document.getElementById('invite-location-group').style.display = places ? '' : 'none';
  document.getElementById('invite-go').disabled = false;
  famAddStep(1);
  showModal('modal-invite');
}

function famAddNext() {
  const name = document.getElementById('invite-name').value.trim();
  if (!name) { toast('Type their name first', 'warning'); document.getElementById('invite-name').focus(); return; }
  const pw = document.getElementById('invite-password');
  if (!pw.value.trim()) pw.value = famSuggestPassword(name);
  if (pw.value.trim().length < 4) { toast('Use a password of at least 4 characters', 'warning'); pw.focus(); return; }
  _famFillWhatTheyGet();
  famAddStep(2);
}

// Step 2, filled for a grown-up or a kid from the household's defaults.
function _famFillWhatTheyGet() {
  const kid = _famIsKid();
  const d = fam.data || {};
  const defs = d.defaults || {};
  const kd = defs.kids || { home: 'kids', channels: 'kids' };
  const named = (d.people || []).filter(p => !p.kids_level);
  const kidsNamed = (d.people || []).filter(p => p.kids_level);
  const same = list => list.map(p => `<option value="${p.id}">The same as ${escHtml(p.name)}</option>`).join('');
  const home = document.getElementById('invite-home');
  home.innerHTML = (kid ? '<option value="kids">Ready-made kids home</option>' : '') +
    '<option value="default">The default home (My Home Screen)</option>' + same(kid ? kidsNamed : named);
  home.value = String(kid ? kd.home : (defs.home_from || 'default'));
  if (!home.value) home.value = kid ? 'kids' : 'default';
  home.onchange = _famHomeHint;
  _famHomeHint();
  const ch = document.getElementById('invite-channels');
  const nfav = n => `${n} favourite${n === 1 ? '' : 's'}`;
  ch.innerHTML = kid
    ? '<option value="kids">The kids channels</option><option value="none">None yet (they star their own)</option>'
    : '<option value="none">None yet (they star their own)</option>' +
      named.filter(p => p.favourite_count).map(p => `<option value="${p.id}">${escHtml(p.name)}’s favourites (${nfav(p.favourite_count)})</option>`).join('');
  ch.value = String(kid ? kd.channels : (defs.channels_from || 'none'));
  if (!ch.value) ch.value = kid ? 'kids' : 'none';
  document.getElementById('invite-livetv').checked = true;
  document.getElementById('invite-requests-group').style.display = kid ? 'none' : '';
  document.querySelector('input[name="invite-requests"][value="ask"]').checked = true;
  famLiveTvChanged();
  _famAsk();
}

function _famHomeHint() {
  const v = document.getElementById('invite-home').value;
  document.getElementById('invite-home-hint').textContent = v === 'kids'
    ? 'Continue Watching, Kids Movies and Kids Shows: cartoons and family films, only what their age limit allows.'
    : v === 'default' ? 'The home you set under My Home Screen → Default for everyone. They can change theirs.'
    : 'The same rows as that person’s home. They can change theirs.';
}

function famLiveTvChanged() {
  const on = document.getElementById('invite-livetv').checked;
  const kid = _famIsKid();
  const d = fam.data || {};
  const n = kid ? d.kids_channels || 0 : d.pool_channels || 0;
  document.getElementById('invite-channels-group').style.display = on && n ? '' : 'none';
  document.getElementById('invite-livetv-hint').textContent = !on ? 'No Live TV for them. You can turn it on later under More.'
    : kid ? (n ? `The ${n} kids channel${n === 1 ? '' : 's'} you picked, nothing else.` : 'No kids channels picked yet, so no Live TV until you choose some (Kids channels, below the list).')
    : `All ${n} channel${n === 1 ? '' : 's'}; they star their favourites.`;
}

// Asked once: the addresses the message gives, while they don't all work
// away from home. Filled with what Tentacle knows (this browser's address
// for Tentacle when it knows none a browser can open).
function _famAsk() {
  const a = fam.data?.addresses || {};
  const box = document.getElementById('invite-ask');
  box.style.display = a.ask ? '' : 'none';
  if (!a.ask) return;
  const t = a.tentacle || location.origin;
  document.getElementById('invite-ask-tentacle').value = t;
  document.getElementById('invite-ask-jellyfin').value = a.jellyfin || '';
  document.getElementById('invite-ask-why').textContent = a.tentacle_away && a.jellyfin_away
    ? 'Family open these to sign in and to watch.'
    : 'These work only on your home network. If family can reach your server from the internet too (a domain or a tunnel), type those addresses instead. You can change them later in Settings → Services.';
}

async function _famSaveAsk() {
  const a = fam.data?.addresses || {};
  if (!a.ask) return true;
  const t = document.getElementById('invite-ask-tentacle').value.trim();
  const j = document.getElementById('invite-ask-jellyfin').value.trim();
  try {
    fam.data.addresses = await api('/api/family/addresses', { method: 'PUT',
      body: { tentacle: t || null, jellyfin: j || null } });
    return true;
  } catch (e) {
    toast(e.message, 'error', 6000);
    return false;
  }
}

async function famInvite() {
  const name = document.getElementById('invite-name').value.trim();
  if (!name) { famAddStep(1); toast('Type their name first', 'warning'); return; }
  const btn = document.getElementById('invite-go');
  btn.disabled = true;
  if (!(await _famSaveAsk())) { btn.disabled = false; return; }
  const kid = _famIsKid();
  const liveTv = document.getElementById('invite-livetv').checked;
  try {
    fam.invited = await api('/api/family/invite', { method: 'POST', body: {
      name,
      password: document.getElementById('invite-password').value,
      kids_level: kid ? document.getElementById('invite-kids').value : null,
      request_mode: kid ? 'ask' : document.querySelector('input[name="invite-requests"]:checked').value,
      location: document.getElementById('invite-location').value || null,
      live_tv: liveTv,
      home: document.getElementById('invite-home').value,
      channels: liveTv ? document.getElementById('invite-channels').value : 'none',
    } });
  } catch (e) {
    toast(e.message, 'error', 6000);
    btn.disabled = false;
    return;
  }
  famRenderInviteCard(fam.invited.invite, `${fam.invited.user.name}’s account is ready. Send them this message: it says what Tentacle is, how to sign in and where to watch.`);
  loadPeople();
}

function famRenderInviteCard(card, lead) {
  document.getElementById('invite-title').textContent = fam.invited ? 'Add someone' : `${card.name}’s new password`;
  document.getElementById('invite-steps').style.display = fam.invited ? '' : 'none';
  famAddStep(3);
  document.getElementById('invite-card-lead').textContent = lead;
  document.getElementById('invite-message').value = card.message || '';
  document.getElementById('invite-card-note').textContent = 'This is the only time the password is shown. You can give them a new one under People.';
  document.getElementById('invite-share').style.display = navigator.share ? '' : 'none';
  fam.lastCard = card;
}

function famCopyInvite() { if (fam.lastCard) famCopy(fam.lastCard.message || ''); }

async function famShareInvite() {
  if (!fam.lastCard || !navigator.share) return;
  try {
    await navigator.share({ title: 'Your Tentacle account', text: fam.lastCard.message || '' });
  } catch (e) {
    if (e && e.name !== 'AbortError') famCopyInvite();
  }
}

function famInviteTvCode() {
  const id = fam.invited?.user?.id;
  closeModal('modal-invite');
  famShowTvCode(id || null);
}

async function famNewPassword(id) {
  const p = _famPerson(id);
  if (!p || !confirm(`Give ${p.name} a new password? The old one stops working, and their TVs stay signed in.`)) return;
  try {
    const card = await api(`/api/family/people/${id}/password`, { method: 'POST' });
    fam.invited = null;
    famRenderInviteCard(card, `${p.name}’s old password has stopped working. Send them this:`);
    showModal('modal-invite');
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Sign in a TV ─────────────────────────────────────────────────────────

function famShowTvCode(id) {
  fam.tvFor = id;
  const p = id ? _famPerson(id) : null;
  document.getElementById('tvcode-title').textContent = p ? `Sign in ${p.name}’s TV` : 'Sign in a TV';
  document.getElementById('tvcode-input').value = '';
  const res = document.getElementById('tvcode-result');
  res.textContent = '';
  res.className = 'fam-result';
  document.getElementById('tvcode-go').disabled = false;
  if (typeof closeSidebar === 'function') closeSidebar();
  showModal('modal-tvcode');
  setTimeout(() => document.getElementById('tvcode-input').focus(), 50);
}

async function famSubmitTvCode() {
  const code = document.getElementById('tvcode-input').value.trim();
  const res = document.getElementById('tvcode-result');
  if (!code) { res.className = 'fam-result bad'; res.textContent = 'Type the code the TV shows.'; return; }
  const btn = document.getElementById('tvcode-go');
  btn.disabled = true;
  try {
    const url = fam.tvFor ? `/api/family/people/${fam.tvFor}/tv-code` : '/api/family/tv-code';
    const r = await api(url, { method: 'POST', body: { code } });
    res.className = 'fam-result good';
    res.textContent = `Done. The TV is signed in as ${r.name}.`;
  } catch (e) {
    res.className = 'fam-result bad';
    res.textContent = e.message;
  }
  btn.disabled = false;
}

// ── Kids channels ────────────────────────────────────────────────────────

async function famShowKidsChannels() {
  try {
    fam.kids = await api('/api/family/kids-channels');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  fam.kidsChosen = new Set(fam.kids.keys);
  document.getElementById('kidsch-search').value = '';
  famRenderKidsChannels();
  showModal('modal-kidschannels');
}

function famRenderKidsChannels() {
  const q = (document.getElementById('kidsch-search').value || '').trim().toLowerCase();
  const el = document.getElementById('kidsch-list');
  const rows = (fam.kids?.channels || []).filter(c => !q || String(c.name || '').toLowerCase().includes(q));
  el.innerHTML = rows.length ? rows.map((c, i) => `
    <label class="fam-ch"><input type="checkbox" data-i="${i}"${fam.kidsChosen.has(c.key) ? ' checked' : ''}>
      <span>${escHtml(c.name)}</span></label>`).join('')
    : `<div class="fam-hint">${q ? 'No channel matches.' : 'Live TV has no channels yet.'}</div>`;
  el.querySelectorAll('input[type=checkbox]').forEach(box => {
    box.onchange = () => {
      const c = rows[parseInt(box.dataset.i, 10)];
      if (box.checked) fam.kidsChosen.add(c.key); else fam.kidsChosen.delete(c.key);
      _famKidsCount();
    };
  });
  _famKidsCount();
}

function _famKidsCount() {
  const n = fam.kidsChosen.size;
  document.getElementById('kidsch-count').textContent = `${n} picked`;
}

async function famSaveKidsChannels() {
  // Keep the household's order: it becomes the kids' channel numbers.
  const keys = (fam.kids?.channels || []).map(c => c.key).filter(k => fam.kidsChosen.has(k));
  try {
    await api('/api/family/kids-channels', { method: 'PUT', body: { keys } });
    toast(keys.length ? `Kids see ${keys.length} channel${keys.length === 1 ? '' : 's'}. TVs follow in a minute or two.` : 'Kids have no Live TV now');
    closeModal('modal-kidschannels');
  } catch (e) {
    toast(e.message, 'error');
  }
  loadPeople();
}

// ── Requests (admins) ────────────────────────────────────────────────────

async function loadRequests() {
  try {
    fam.requests = await api('/api/requests');
  } catch (e) {
    document.getElementById('req-list').innerHTML = `<div class="fam-error">Couldn't load the requests: ${escHtml(e.message)}</div>`;
    return;
  }
  const n = fam.requests.pending.length;
  document.getElementById('req-count-pending').textContent = n || '';
  const b = document.getElementById('requests-badge');
  if (b) { b.textContent = n; b.style.display = n ? '' : 'none'; }
  famRenderRequests();
}

function famReqTab(tab) {
  fam.reqTab = tab;
  document.querySelectorAll('[data-reqtab]').forEach(b => b.classList.toggle('active', b.dataset.reqtab === tab));
  if (fam.requests) famRenderRequests();
  setRoute('requests', tab === 'pending' ? null : tab);
}

const FAM_KIND = { movie: 'Movie', series: 'Show', album: 'Album' };

function famRenderRequests() {
  const el = document.getElementById('req-list');
  const list = fam.reqTab === 'pending'
    ? fam.requests.pending.concat(fam.requests.decided.filter(r => r.status === 'failed'))
    : fam.requests.decided.filter(r => r.status !== 'failed');
  if (!list.length) {
    el.innerHTML = fam.reqTab === 'pending'
      ? '<div class="empty-state"><p>Nothing waiting.</p><p class="fam-hint">When someone asks for a movie or show, it shows up here and you get a message.</p></div>'
      : '<div class="empty-state"><p>No answers in the last 30 days.</p></div>';
    return;
  }
  el.innerHTML = list.map(r => {
    const poster = r.poster_path
      ? `<img class="req-poster" alt="" loading="lazy" src="${escHtml('https://image.tmdb.org/t/p/w154' + r.poster_path)}">`
      : `<div class="req-poster none">${escHtml((FAM_KIND[r.kind] || '?').charAt(0))}</div>`;
    const what = [FAM_KIND[r.kind] || r.kind, r.year, r.episodes ? `${r.episodes} episode${r.episodes === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ');
    let side;
    if (r.status === 'pending' || r.status === 'failed') {
      side = `<div class="req-buttons">
        <button class="btn btn-primary btn-sm" onclick="famApprove(${r.id})">${r.status === 'failed' ? 'Try again' : 'Approve'}</button>
        <button class="btn btn-secondary btn-sm" onclick="famDecline(${r.id})">Decline</button></div>`;
    } else {
      const label = r.status === 'approved' ? `<span class="badge badge-green">Approved</span>` : `<span class="badge badge-gray">Declined</span>`;
      side = `<div class="req-answer">${label}<div class="fam-hint">${escHtml(r.decided_by)} &middot; ${escHtml(_famAgo(r.decided_at))}</div></div>`;
    }
    const note = r.status === 'failed'
      ? `<div class="req-note bad">Approved, but it didn’t go through: ${escHtml(r.note)}</div>`
      : r.note ? `<div class="req-note">“${escHtml(r.note)}”</div>` : '';
    return `<div class="card req-item">
      ${poster}
      <div class="req-text">
        <div class="req-title">${escHtml(r.title)}</div>
        <div class="req-what">${escHtml(what)}</div>
        <div class="req-who">${escHtml(r.requested_by)} asked ${escHtml(_famAgo(r.created_at))}</div>
        ${note}
      </div>
      ${side}
    </div>`;
  }).join('');
}

async function famApprove(id) {
  try {
    const r = await api(`/api/requests/${id}/approve`, { method: 'POST' });
    if (r.ok) toast('Approved: it’s downloading');
    else toast(`It didn’t go through: ${r.detail}`, 'error', 8000);
  } catch (e) {
    toast(e.message, 'error');
  }
  loadRequests();
}

async function famDecline(id) {
  const r = (fam.requests?.pending || []).concat(fam.requests?.decided || []).find(x => x.id === id);
  const note = prompt(`Decline ${r ? r.title : 'this'}? You can add a short reason for ${r ? r.requested_by : 'them'} (optional):`, '');
  if (note === null) return;
  try {
    await api(`/api/requests/${id}/decline`, { method: 'POST', body: { note } });
    toast('Declined');
  } catch (e) {
    toast(e.message, 'error');
  }
  loadRequests();
}

// ── Your requests (everyone, Discover → Activity) ───────────────────────

async function famLoadMyRequests() {
  const el = document.getElementById('my-requests');
  if (!el) return;
  let d;
  try { d = await api('/api/requests/mine'); } catch (e) { el.style.display = 'none'; return; }
  const rows = d.pending.concat(d.decided.filter(r => r.status === 'declined'));
  if (!rows.length) { el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = '<div class="card-title">Your requests</div>' + rows.map(r => {
    const status = r.status === 'pending'
      ? `<span class="badge badge-amber">Waiting for ${escHtml((state.requestMode && state.requestMode.approver) || 'your admin')}</span>`
      : `<span class="badge badge-gray">${escHtml(r.decided_by || 'Your admin')} said no</span>`;
    const take = r.status === 'pending'
      ? `<button class="btn btn-secondary btn-sm" onclick="famTakeBack(${r.id})">Take back</button>` : '';
    const note = r.note ? `<div class="fam-hint">“${escHtml(r.note)}”</div>` : '';
    return `<div class="my-req"><div class="my-req-title">${escHtml(r.title)}${r.year ? ' (' + escHtml(r.year) + ')' : ''}${note}</div>${status}${take}</div>`;
  }).join('');
}

async function famTakeBack(id) {
  try {
    await api(`/api/requests/mine/${id}`, { method: 'DELETE' });
    toast('Taken back');
  } catch (e) {
    toast(e.message, 'error');
  }
  famLoadMyRequests();
}
