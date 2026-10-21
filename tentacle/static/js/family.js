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
  famRenderPeople();
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
    if (p.disabled) chips.push('<span class="badge badge-gray">Turned off in Jellyfin</span>');
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
        <label class="fam-ctl fam-ctl-admin" title="${escHtml(adminTitle)}"><span class="fam-ctl-label">Admin</span>
          <input type="checkbox"${p.is_admin ? ' checked' : ''}${adminLocked ? ' disabled' : ''} onchange="famToggleAdmin(${p.id}, this.checked)"></label>
        <label class="fam-ctl fam-ctl-admin" title="Shown on the sign-in screen at home, to pick instead of typing a name"><span class="fam-ctl-label">On sign-in screen</span>
          <input type="checkbox"${p.on_sign_in ? ' checked' : ''} onchange="famSetOnSignIn(${p.id}, this.checked)"></label>
        <div class="fam-actions">
          <button class="btn btn-secondary btn-sm" onclick="famShowTvCode(${p.id})">Sign in a TV</button>
          ${p.is_admin ? '' : `<button class="btn btn-secondary btn-sm" onclick="famNewPassword(${p.id})">New password</button>`}
        </div>
      </div>
    </div>`;
  }).join('') || '<div class="empty-state"><p>Nobody yet.</p></div>';

  // New people start with
  const home = document.getElementById('fam-default-home');
  const chan = document.getElementById('fam-default-channels');
  const named = d.people.filter(p => !p.kids_level);
  home.innerHTML = '<option value="">Tentacle’s standard rows</option>' +
    named.map(p => `<option value="${p.id}"${d.defaults.home_from === p.id ? ' selected' : ''}>The same as ${escHtml(p.name)}</option>`).join('');
  const lists = named.filter(p => p.has_own_channels);
  chan.innerHTML = '<option value="">All channels</option>' +
    lists.map(p => `<option value="${p.id}"${d.defaults.channels_from === p.id ? ' selected' : ''}>The same as ${escHtml(p.name)}’s list</option>`).join('');
  const names = d.kids_channel_names || [];
  document.getElementById('fam-kids-summary').textContent = d.kids_channels
    ? `Kids see ${d.kids_channels === 1 ? 'one channel' : d.kids_channels + ' channels'} in Live TV: ${names.join(', ')}${d.kids_channels > names.length ? ' and more' : ''}.`
    : 'None picked yet, so kids have no Live TV.';
  document.getElementById('fam-tv-address').textContent = d.addresses.tv_app || 'Set Tentacle’s address in Settings → Services first';
  document.getElementById('fam-tv-newest').textContent = d.tv_app && d.tv_app.newest ? `Newest version: ${d.tv_app.newest}.` : '';
}

function _famPerson(id) { return (fam.data?.people || []).find(p => p.id === id); }

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
    fam.data.defaults = await api('/api/family/defaults', { method: 'PUT',
      body: { home_from: v('fam-default-home'), channels_from: v('fam-default-channels') } });
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

// ── Invite ───────────────────────────────────────────────────────────────

function famShowInvite() {
  document.getElementById('invite-title').textContent = 'Invite someone';
  document.getElementById('invite-step-form').style.display = '';
  document.getElementById('invite-step-card').style.display = 'none';
  document.getElementById('invite-name').value = '';
  const sel = document.getElementById('invite-kids');
  sel.innerHTML = '<option value="">No limit (a grown-up)</option>' +
    (fam.data?.kids_levels || []).map(l => `<option value="${escHtml(l.id)}">${escHtml(l.name)} (${escHtml(l.allows)})</option>`).join('');
  sel.onchange = () => {
    const kid = !!sel.value;
    document.getElementById('invite-direct-row').style.display = kid ? 'none' : '';
    if (kid) document.querySelector('input[name="invite-requests"][value="ask"]').checked = true;
  };
  sel.onchange();
  document.querySelector('input[name="invite-requests"][value="ask"]').checked = true;
  const d = fam.data?.defaults || {};
  const name = id => (_famPerson(id) || {}).name;
  const bits = [];
  bits.push(d.home_from && name(d.home_from) ? `the same home screen as ${name(d.home_from)}` : 'Tentacle’s standard home screen');
  bits.push(d.channels_from && name(d.channels_from) ? `${name(d.channels_from)}’s Live TV channels` : 'all Live TV channels');
  document.getElementById('invite-defaults-hint').textContent = `They start with ${bits.join(' and ')} (change that on this page).`;
  document.getElementById('invite-go').disabled = false;
  showModal('modal-invite');
  setTimeout(() => document.getElementById('invite-name').focus(), 50);
}

async function famInvite() {
  const name = document.getElementById('invite-name').value.trim();
  if (!name) { toast('Type their name first', 'warning'); return; }
  const btn = document.getElementById('invite-go');
  btn.disabled = true;
  try {
    fam.invited = await api('/api/family/invite', { method: 'POST', body: {
      name,
      kids_level: document.getElementById('invite-kids').value || null,
      request_mode: document.querySelector('input[name="invite-requests"]:checked').value,
    } });
  } catch (e) {
    toast(e.message, 'error', 6000);
    btn.disabled = false;
    return;
  }
  famRenderInviteCard(fam.invited.invite, `${fam.invited.user.name}’s account is ready. Give them this (it’s the only time the password is shown):`);
  loadPeople();
}

function famRenderInviteCard(card, lead) {
  document.getElementById('invite-title').textContent = `Welcome, ${card.name}`;
  document.getElementById('invite-step-form').style.display = 'none';
  document.getElementById('invite-step-card').style.display = '';
  document.getElementById('invite-card-lead').textContent = lead;
  const row = (k, v) => `<div class="fam-card-row"><span>${escHtml(k)}</span><strong>${escHtml(v)}</strong></div>`;
  document.getElementById('invite-card').innerHTML =
    row('Name', card.name) + row('Password', card.password) +
    (card.jellyfin_address ? row('Server', card.jellyfin_address) : '') +
    `<div class="fam-card-steps"><strong>On the TV</strong><ol>
      <li>Install <strong>Downloader</strong> from the TV’s app store, open it and type <strong>${escHtml(card.tv_app_address || '')}</strong>.</li>
      <li>Open Tentacle, add the server above and pick your name.</li>
      <li>Type the password, or choose Use Quick Connect and give the code to whoever set you up.</li>
    </ol></div>`;
  fam.lastCard = card;
}

function _famCardText(card) {
  return [
    `Your Tentacle account`,
    `Name: ${card.name}`, `Password: ${card.password}`,
    card.jellyfin_address ? `Server: ${card.jellyfin_address}` : '',
    '', 'On the TV:',
    `1. Install Downloader from the TV's app store, open it and type ${card.tv_app_address}`,
    '2. Open Tentacle, add the server above and pick your name.',
    '3. Type the password, or choose Use Quick Connect and give the code to whoever set you up.',
  ].filter((l, i) => l !== '' || i > 3).join('\n');
}

function famCopyInvite() { if (fam.lastCard) famCopy(_famCardText(fam.lastCard)); }

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
    famRenderInviteCard(card, `${p.name}’s new password. Give them this:`);
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
