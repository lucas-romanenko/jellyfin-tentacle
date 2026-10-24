// Tentacle - Recordings (everyone): follow teams, keyword rules, and what
// will record.
//
// My teams: pick a league, tap a team; Tentacle finds its games in the TV
// guide (the team's names, city, arena; event channels' slots; the game
// after its pregame show) and books them as ordinary Jellyfin recordings.
// A new team or rule starts in preview: it shows what it would record and
// books nothing until "Record their games" is switched on.
// Upcoming: what will record, why, on which channel, and what waits.
// Recorded: what the rules recorded, skipped or couldn't.
//
// Everything shown comes from guides and channel names: text, never markup
// (every value goes through escHtml).

const rec = {
  tab: 'upcoming',
  rules: null,        // GET /api/live/recording-rules
  leagues: null,      // GET /api/live/recording-rules/leagues
  upcoming: null,     // GET /api/live/recordings/upcoming
  history: null,
  league: null,       // the league open in the team picker
  find: '',           // its search box (long lists)
  edit: null,         // the rule in the editor: {id?, body}
  previewSeq: 0,
  previewTimer: null,
  busy: false,
};

const REC_STATE = {
  preview: ['Preview', 'badge-gray', 'Would record (preview: nothing is booked)'],
  booked: ['Will record', 'badge-green', ''],
  recording: ['Recording now', 'badge-red', ''],
  waiting: ['Waiting', 'badge-amber', ''],
  covered: ['Set in Jellyfin', 'badge-blue', ''],
  failed: ['Not booked', 'badge-red', ''],
  lost: ['Booking again', 'badge-amber', ''],
  ended: ['Recorded', 'badge-green', ''],
  skipped: ['Skipped', 'badge-gray', ''],
};
const REC_BEFORE = [0, 2, 5, 10, 15, 30];
const REC_AFTER = [0, 10, 15, 30, 45, 60, 90, 120, 180];
const REC_PLAYOFF = [0, 30, 60, 90, 120];

async function loadRecordings() {
  try {
    const [rules, up] = await Promise.all([api('/api/live/recording-rules'), api('/api/live/recordings/upcoming')]);
    rec.rules = rules;
    rec.upcoming = up;
    if (rec.tab === 'history') rec.history = await api('/api/live/recordings/history');
  } catch (e) {
    if (!e.sessionExpired) toast('Recordings could not be loaded: ' + e.message, 'error');
    return;
  }
  recRender();
}

function recTab(name) {
  rec.tab = name;
  document.querySelectorAll('#rec-tabs .live-tab').forEach(b => b.classList.toggle('active', b.dataset.rectab === name));
  document.querySelectorAll('#page-recordings .rec-panel').forEach(p => { p.style.display = p.id === 'rec-panel-' + name ? '' : 'none'; });
  if (name === 'teams' && !rec.leagues) recLoadLeagues();
  if (name === 'history') api('/api/live/recordings/history').then(d => { rec.history = d; recRenderHistory(); }).catch(() => {});
  recRender();
}

async function recLoadLeagues() {
  try {
    rec.leagues = (await api('/api/live/recording-rules/leagues')).leagues;
  } catch (e) {
    if (!e.sessionExpired) toast('The leagues could not be loaded: ' + e.message, 'error');
    return;
  }
  recRenderTeams();
}

function recRender() {
  if (!rec.rules) return;
  const up = rec.upcoming || { rules: [], jellyfin: [] };
  const n = up.rules.filter(b => b.state !== 'preview').length + up.jellyfin.length;
  document.getElementById('rec-count-upcoming').textContent = n || '';
  const teams = rec.rules.rules.filter(r => r.kind !== 'keywords').length;
  document.getElementById('rec-count-teams').textContent = teams || '';
  const words = rec.rules.rules.filter(r => r.kind === 'keywords').length;
  document.getElementById('rec-count-rules').textContent = words || '';
  recRenderUpcoming();
  if (rec.tab === 'teams') recRenderTeams();
  if (rec.tab === 'rules') recRenderRules();
  if (rec.tab === 'history') recRenderHistory();
}

// ── Times, in the viewer's own time zone ───────────────────────────────────

function _recDay(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tomorrow';
  if (diff === -1) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
}
function _recTime(d) { return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
function _recSpan(a) {
  const s = new Date(a.start), e = new Date(a.stop);
  return `${_recTime(s)}–${_recTime(e)}`;
}
function _recWhen(a) { const s = new Date(a.start); return `${_recDay(s)} ${_recTime(s)}`; }

function _recPill(state, extra) {
  const [label, cls] = REC_STATE[state] || [state, 'badge-gray'];
  return `<span class="badge ${cls} rec-pill">${escHtml(extra || label)}</span>`;
}

// ── Upcoming ───────────────────────────────────────────────────────────────

function recRenderUpcoming() {
  const el = document.getElementById('rec-panel-upcoming');
  const up = rec.upcoming || { rules: [], jellyfin: [] };
  const all = up.rules.map(b => ({ ...b, src: 'rule' })).concat(up.jellyfin.map(t => ({ ...t, src: 'jellyfin' })));
  all.sort((a, b) => a.start.localeCompare(b.start));
  // What really records first; what teams and rules in preview would, folded away.
  const real = all.filter(it => it.state !== 'preview');
  const preview = all.filter(it => it.state === 'preview');
  const parts = [];
  if (real.length) {
    parts.push(`<div class="card rec-card rec-list">${_recByDay(real)}</div>`);
  } else {
    const none = rec.rules && rec.rules.rules.some(r => r.mode === 'record')
      ? 'Nothing to record in the TV guide yet. The guide reaches a day or two ahead: games appear as it fills in.'
      : rec.rules && rec.rules.rules.length
        ? 'Nothing set to record yet: your teams and rules are in preview. Switch on “Record their games” under My teams when the preview looks right.'
        : 'Nothing set to record. Follow a team under My teams, or set a rule under Rules.';
    parts.push(`<div class="card rec-card"><div class="mych-none">${none}</div></div>`);
  }
  if (preview.length) {
    parts.push(`<details class="card rec-card rec-list rec-preview-box"${real.length ? '' : ' open'}>
      <summary>In preview: would record ${preview.length} <span class="mych-hint">nothing is booked for these</span></summary>
      ${_recByDay(preview)}</details>`);
  }
  parts.push(`<p class="rec-foot">Recordings are in Jellyfin (Live TV → Recordings), for everyone. Times are yours (${escHtml(Intl.DateTimeFormat().resolvedOptions().timeZone || 'local')}).</p>`);
  el.innerHTML = parts.join('');
}

function _recByDay(items) {
  let day = '';
  const rows = [];
  for (const it of items) {
    const d = _recDay(new Date(it.start));
    if (d !== day) { day = d; rows.push(`<div class="rec-day">${escHtml(d)}</div>`); }
    rows.push(_recUpcomingRow(it));
  }
  return rows.join('');
}

function _recUpcomingRow(it) {
  const meta = [];
  const chan = it.from_event ? 'Event channel' : (it.channel || '');
  meta.push(`<span class="rec-chan">${escHtml(chan)}</span>`);
  if (it.src === 'rule') {
    if (it.rule) meta.push(`<span class="via">${escHtml(it.rule)}</span>`);
    // Why, only where the title doesn't say it ("NHL Hockey" after the Leafs' pregame show)
    if (it.why && /^(same time|follows)/.test(it.why)) meta.push(`<span class="rec-why">${escHtml(it.why.replace(/ \(.*\)$/, ''))}</span>`);
    if (it.state === 'waiting' || it.state === 'failed' || it.state === 'lost') meta.push(`<span class="warn">${escHtml(it.reason || '')}</span>`);
  } else {
    meta.push(`<span>${escHtml(it.reason || 'Set in Jellyfin')}</span>`);
  }
  let pill = '';
  if (it.src === 'jellyfin') pill = _recPill(it.state === 'recording' ? 'recording' : 'covered');
  else if (it.state !== 'preview') pill = _recPill(it.state);
  const skip = it.src === 'rule' && ['preview', 'booked', 'waiting', 'failed'].includes(it.state) && rec.rules?.can_record
    ? `<button class="mych-icon rec-skip" data-id="${it.id}" title="Skip this showing" aria-label="Skip ${escHtml(it.title)}">✕</button>`
    : '';
  const sub = it.sub_title ? ` <span class="rec-subt">${escHtml(it.sub_title)}</span>` : '';
  return `<div class="mych-row rec-row">
    <span class="rec-time">${escHtml(_recSpan(it))}</span>
    <span class="mych-text"><span class="mych-name">${escHtml(it.title || '')}${sub}</span>
      <span class="mych-meta">${meta.join('')}</span></span>
    ${pill}${skip}</div>`;
}

async function recSkip(id) {
  const b = (rec.upcoming?.rules || []).find(x => x.id === id);
  if (!b || rec.busy) return;
  const what = b.state === 'booked' ? 'Its recording is cancelled and' : 'It';
  if (!confirm(`Skip “${b.title}” (${_recWhen(b)} on ${b.channel})?\n\n${what} won't be booked again. You can bring it back under Recorded.`)) return;
  rec.busy = true;
  try {
    await api(`/api/live/recordings/${id}/skip`, { method: 'POST' });
    _recStatus('ok', `Skipped ${b.title}.`);
    await loadRecordings();
  } catch (e) {
    if (!e.sessionExpired) toast(e.message, 'warning', 6000);
  } finally { rec.busy = false; }
}

// ── My teams ───────────────────────────────────────────────────────────────

function recRenderTeams() {
  const el = document.getElementById('rec-panel-teams');
  if (!rec.rules) return;
  const can = rec.rules.can_record;
  const mine = rec.rules.rules.filter(r => r.kind !== 'keywords');
  const followed = mine.length ? mine.map(_recTeamRow).join('') : `<div class="mych-none">No teams yet.${can ? ' Pick a league below, then your team.' : ''}</div>`;
  let picker = '';
  if (can) {
    const lgs = rec.leagues || [];
    const chips = lgs.map(l => `<button class="mych-pick${rec.league === l.id ? ' on' : ''}" data-league="${escHtml(l.id)}">${escHtml(l.name)}</button>`).join('');
    let teams = '';
    const lg = lgs.find(l => l.id === rec.league);
    if (lg && lg.kind === 'series') {
      const have = mine.some(r => r.kind === 'series' && r.league === lg.id && r.mine);
      teams = `<div class="rec-teams"><button class="rec-team${have ? ' on' : ''}" data-series="${escHtml(lg.id)}">${have ? '✓ ' : ''}Follow ${escHtml(lg.name)}</button></div>`;
    } else if (lg) {
      // a long list (211 national teams) gets a search box: any of a team's names
      const find = lg.teams.length > 40
        ? `<div class="rec-find"><input class="form-input" id="rec-find" type="search" placeholder="Find a team" value="${escHtml(rec.find || '')}" autocomplete="off"></div>` : '';
      teams = `${find}<div class="rec-teams">${lg.teams.map(t => {
        const have = mine.some(r => r.kind === 'team' && r.league === lg.id && r.team === t.id && r.mine);
        const words = _recFold([t.name, t.short, t.abbr, ...(t.also || [])].join(' '));
        const hide = find && !_recFound(words, rec.find) ? ' hidden' : '';
        return `<button class="rec-team${have ? ' on' : ''}" data-team="${escHtml(t.id)}" data-find="${escHtml(words)}" title="${escHtml(t.name)}"${hide}>`
          + `<span class="abbr">${escHtml(t.abbr || '')}</span><span>${have ? '✓ ' : ''}${escHtml(t.name)}</span></button>`;
      }).join('')}</div>`;
    }
    picker = `<div class="card rec-card">
      <div class="mych-step-title">Add a team</div>
      <p class="mych-hint">Pick a league, then tap your team. It starts in preview: you see which games it would record before anything is booked.</p>
      <div class="mych-picks">${chips || '<span class="mych-hint">Loading…</span>'}</div>${teams}</div>`;
  } else {
    picker = `<div class="card rec-card"><div class="mych-none">Your account can&rsquo;t set recordings. An admin can allow it under People (Can record).</div></div>`;
  }
  el.innerHTML = `<div class="card rec-card rec-list">${followed}</div>${picker}`;
}

function _recFold(v) {
  return String(v || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
}

// the start of any of a team's names or words ("can" finds Canada, not Africa)
function _recFound(words, q) {
  q = _recFold(q);
  return !q || (' ' + words).includes(' ' + q);
}

function recFindTeam(v) {
  rec.find = v;
  document.querySelectorAll('#rec-panel-teams .rec-team[data-find]').forEach(b => { b.hidden = !_recFound(b.dataset.find, v); });
}

function _recModeText(r) {
  return r.mode === 'record' ? (r.kind === 'keywords' ? 'Records' : 'Records their games') : 'Preview only';
}

function _recTeamRow(r) {
  const meta = [`<span>${escHtml(r.league_name || '')}</span>`];
  if (r.kind === 'team' && r.home_away !== 'both') meta.push(`<span>${r.home_away === 'home' ? 'Home games' : 'Away games'}</span>`);
  meta.push(`<span>${r.upcoming ? r.upcoming + ' coming up' : 'none in the guide yet'}</span>`);
  if (!r.mine && r.owner) meta.push(`<span class="via">${escHtml(r.owner)}'s</span>`);
  return _recRuleRow(r, r.team_name || r.name, meta);
}

function _recRuleRow(r, name, meta) {
  const sw = r.can_edit && rec.rules.can_record
    ? `<label class="rec-switch" title="${r.mode === 'record' ? 'Recording: switch off to go back to preview' : 'Preview: switch on to record'}">
        <input type="checkbox" class="rec-mode" data-id="${r.id}" ${r.mode === 'record' ? 'checked' : ''}>
        <span>${escHtml(_recModeText(r))}</span></label>`
    : `<span class="rec-mode-text">${escHtml(_recModeText(r))}</span>`;
  const edit = `<button class="btn btn-secondary btn-sm rec-edit" data-id="${r.id}">${r.can_edit ? 'Edit' : 'See'}</button>`;
  return `<div class="mych-row rec-rule-row${r.mode === 'record' ? ' on' : ''}">
    <span class="mych-text"><span class="mych-name">${escHtml(name)}</span><span class="mych-meta">${meta.join('')}</span></span>
    ${sw}${edit}</div>`;
}

async function recFollowTeam(league, team) {
  const lg = (rec.leagues || []).find(l => l.id === league);
  const existing = rec.rules.rules.find(r => r.mine && r.league === league && (team ? r.team === team : r.kind === 'series'));
  if (existing) { recOpenEditor(existing.id); return; }
  if (rec.busy) return;
  rec.busy = true;
  try {
    const body = team ? { kind: 'team', league, team } : { kind: 'series', league };
    const r = await api('/api/live/recording-rules', { method: 'POST', body });
    _recStatus('ok', `Following ${r.name} (preview). Here is what it would record.`);
    await loadRecordings();
    recOpenEditor(r.id);
  } catch (e) {
    if (!e.sessionExpired) toast(e.message, 'warning', 6000);
  } finally { rec.busy = false; }
  void lg;
}

async function recSetMode(id, on) {
  const r = rec.rules.rules.find(x => x.id === id);
  if (!r) return;
  if (on && !confirm(`Record ${r.team_name || r.name}${r.kind === 'keywords' ? '' : "'s games"} from now on?\n\nTentacle books them as Jellyfin recordings (for everyone in the household), ${r.pre_padding_minutes} min before and ${r.post_padding_minutes} min after each.`)) {
    recRender();
    return;
  }
  try {
    await api(`/api/live/recording-rules/${id}`, { method: 'PATCH', body: { mode: on ? 'record' : 'preview' } });
    _recStatus('ok', on ? `${r.name}: recording. Its games are booked within a minute.` : `${r.name}: back to preview. Its upcoming recordings are cancelled.`);
    await loadRecordings();
  } catch (e) {
    if (!e.sessionExpired) toast(e.message, 'warning', 6000);
    recRender();
  }
}

// ── Rules (typed words) ────────────────────────────────────────────────────

function recRenderRules() {
  const el = document.getElementById('rec-panel-rules');
  if (!rec.rules) return;
  const rules = rec.rules.rules.filter(r => r.kind === 'keywords');
  const rows = rules.length ? rules.map(r => {
    const meta = [`<span>${escHtml(r.terms.join(r.match_all ? ' + ' : ', '))}</span>`];
    if (r.excludes.length) meta.push(`<span>not: ${escHtml(r.excludes.join(', '))}</span>`);
    meta.push(`<span>${r.upcoming ? r.upcoming + ' coming up' : 'none in the guide yet'}</span>`);
    if (!r.mine && r.owner) meta.push(`<span class="via">${escHtml(r.owner)}'s</span>`);
    return _recRuleRow(r, r.name, meta);
  }).join('') : '<div class="mych-none">No rules yet. A rule records anything whose title has your words: a show, “Grand Prix”, a tournament.</div>';
  const add = rec.rules.can_record ? '<button class="btn btn-primary btn-sm" id="rec-new-rule">New rule</button>' : '';
  el.innerHTML = `<div class="card rec-card rec-list"><div class="mych-panel-head"><div>
      <div class="card-title">Rules</div><div class="mych-hint">Record by words in the TV guide. Whole words; accents and capitals don&rsquo;t matter.</div></div>${add}</div>${rows}</div>`;
}

// ── The editor: options on the left, what it would record on the right ───

function recOpenEditor(id) {
  const r = id ? rec.rules.rules.find(x => x.id === id) : null;
  const body = r ? {
    kind: r.kind, league: r.league, team: r.team, name: r.name, home_away: r.home_away, terms: r.terms,
    excludes: r.excludes, match_all: r.match_all, search_description: r.search_description, channels: r.channels,
    min_minutes: r.min_minutes, pre_padding_minutes: r.pre_padding_minutes, post_padding_minutes: r.post_padding_minutes,
    playoff_extra_minutes: r.playoff_extra_minutes, pregame_next: r.pregame_next, skip_replays: r.skip_replays, mode: r.mode,
  } : { kind: 'keywords', terms: [], excludes: [], match_all: false, search_description: false, home_away: 'both',
    pre_padding_minutes: 5, post_padding_minutes: 10, playoff_extra_minutes: 0, pregame_next: true, skip_replays: true,
    mode: 'preview', min_minutes: null };
  rec.edit = { id: r ? r.id : null, body, canEdit: r ? r.can_edit : true };
  document.getElementById('rec-ed-title').textContent = r ? (r.team_name || r.name) : 'New rule';
  _recEditorForm();
  document.getElementById('rec-ed-preview').innerHTML = '<div class="mych-none">Looking in the guide…</div>';
  document.getElementById('rec-ed-remove').style.display = r && r.can_edit ? '' : 'none';
  document.getElementById('rec-ed-save').style.display = rec.edit.canEdit ? '' : 'none';
  showModal('modal-rec-rule');
  recPreview();
}

function _recSelect(name, values, cur, unit) {
  return `<select class="form-input rec-in" data-k="${name}">${values.map(v =>
    `<option value="${v}"${Number(cur) === v ? ' selected' : ''}>${v} ${unit}</option>`).join('')}</select>`;
}

function _recEditorForm() {
  const b = rec.edit.body;
  const dis = rec.edit.canEdit ? '' : ' disabled';
  const parts = [];
  if (b.kind === 'keywords') {
    parts.push(`<label class="rec-field"><span>Words to look for</span>
      <input class="form-input rec-in" data-k="terms" value="${escHtml((b.terms || []).join(', '))}" placeholder="Grand Prix, Hockey Night in Canada"${dis}>
      <small>Commas between them. Any of them, unless:</small></label>
      <label class="rec-check"><input type="checkbox" class="rec-in" data-k="match_all"${b.match_all ? ' checked' : ''}${dis}> Every word must be there</label>
      <label class="rec-check"><input type="checkbox" class="rec-in" data-k="search_description"${b.search_description ? ' checked' : ''}${dis}> Also look in the description</label>
      <label class="rec-field"><span>Leave out listings with</span>
      <input class="form-input rec-in" data-k="excludes" value="${escHtml((b.excludes || []).join(', '))}" placeholder="Practice, Highlights"${dis}></label>
      <label class="rec-field"><span>Shortest listing</span>
      <input class="form-input rec-in rec-num" data-k="min_minutes" type="number" min="0" max="600" value="${b.min_minutes ?? ''}" placeholder="any"${dis}> <small>minutes</small></label>`);
  } else {
    if (b.kind === 'team') {
      parts.push(`<div class="rec-field"><span>Games</span><div class="rec-seg">${['both', 'home', 'away'].map(v =>
        `<button class="filter-btn rec-ha${b.home_away === v ? ' active' : ''}" data-v="${v}"${dis}>${v === 'both' ? 'All' : v === 'home' ? 'Home' : 'Away'}</button>`).join('')}</div></div>
        <label class="rec-check"><input type="checkbox" class="rec-in" data-k="pregame_next"${b.pregame_next ? ' checked' : ''}${dis}> When only the pregame show names the team, record what follows it</label>`);
    }
    parts.push(`<label class="rec-field"><span>Leave out listings with</span>
      <input class="form-input rec-in" data-k="excludes" value="${escHtml((b.excludes || []).join(', '))}" placeholder="${b.kind === 'series' ? 'Practice, Qualifying' : 'Highlights'}"${dis}></label>`);
  }
  parts.push(`<label class="rec-check"><input type="checkbox" class="rec-in" data-k="skip_replays"${b.skip_replays ? ' checked' : ''}${dis}> Skip replays (record the first showing)</label>
    <div class="rec-field"><span>Start early, keep going</span><div class="rec-pad">${_recSelect('pre_padding_minutes', REC_BEFORE, b.pre_padding_minutes, 'min before')}
      ${_recSelect('post_padding_minutes', REC_AFTER, b.post_padding_minutes, 'min after')}</div>
      ${b.kind !== 'keywords' ? `<div class="rec-pad">${_recSelect('playoff_extra_minutes', REC_PLAYOFF, b.playoff_extra_minutes, 'min more')} <small>for playoff games (overtime)</small></div>` : ''}</div>
    <label class="rec-check rec-record"><input type="checkbox" class="rec-in" data-k="mode"${b.mode === 'record' ? ' checked' : ''}${dis}>
      <span><b>${b.kind === 'keywords' ? 'Record' : 'Record their games'}</b><small>Off: preview only, nothing is booked</small></span></label>`);
  document.getElementById('rec-ed-form').innerHTML = parts.join('');
}

function _recSplit(v) { return String(v || '').split(',').map(s => s.trim()).filter(Boolean); }

function recEditorInput(el) {
  const b = rec.edit.body;
  const k = el.dataset.k;
  if (k === 'terms' || k === 'excludes') b[k] = _recSplit(el.value);
  else if (k === 'mode') b.mode = el.checked ? 'record' : 'preview';
  else if (el.type === 'checkbox') b[k] = el.checked;
  else if (k === 'min_minutes') b[k] = el.value === '' ? null : Number(el.value);
  else b[k] = Number(el.value);
  if (k !== 'mode' && !k.endsWith('padding_minutes') && k !== 'playoff_extra_minutes') {
    clearTimeout(rec.previewTimer);
    rec.previewTimer = setTimeout(recPreview, 450);
  }
}

async function recPreview() {
  const b = rec.edit?.body;
  const el = document.getElementById('rec-ed-preview');
  if (!b) return;
  if (b.kind === 'keywords' && !(b.terms || []).length) {
    el.innerHTML = '<div class="mych-none">Type the words to look for: what they would record shows here.</div>';
    return;
  }
  const seq = ++rec.previewSeq;
  let d;
  try {
    d = await api('/api/live/recording-rules/preview', { method: 'POST', body: { ...b, id: rec.edit.id, mode: 'preview' } });
  } catch (e) {
    if (seq === rec.previewSeq) el.innerHTML = `<div class="mych-none">${escHtml(e.message)}</div>`;
    return;
  }
  if (seq !== rec.previewSeq) return;
  el.innerHTML = _recPreviewHtml(d);
}

function _recPreviewHtml(d) {
  const until = d.guide_until ? `${_recDay(new Date(d.guide_until))} ${_recTime(new Date(d.guide_until))}` : '';
  const rec_ = d.events.filter(e => e.state === 'record');
  const wait = d.events.filter(e => e.state === 'waiting');
  const replays = d.events.filter(e => e.state === 'replay');
  const out = [`<div class="rec-pv-head">In the TV guide now${until ? ` (to ${escHtml(until)})` : ''}: `
    + `<b>${rec_.length ? `would record ${rec_.length}` : 'nothing to record yet'}</b></div>`];
  const row = (e, cls) => {
    const a = e.airing;
    const also = e.also.length ? `<span>also on ${escHtml(e.also.slice(0, 3).map(x => x.airing.channel).join(', '))}${e.also.length > 3 ? '…' : ''}</span>` : '';
    const home = e.home === true ? '<span>home</span>' : e.home === false ? '<span>away</span>' : '';
    return `<div class="rec-pv-row ${cls}"><div class="rec-pv-when">${escHtml(_recWhen(a))}</div>
      <div class="rec-pv-what"><b>${escHtml(a.title)}</b> <span class="rec-chan">${escHtml(a.channel)}</span>
      <div class="mych-meta"><span>${escHtml(e.why)}</span>${home}${also}${e.reason ? `<span class="warn">${escHtml(e.reason)}</span>` : ''}</div></div></div>`;
  };
  out.push(...rec_.map(e => row(e, '')), ...wait.map(e => row(e, 'wait')));
  if (!rec_.length && !wait.length) out.push('<div class="mych-none">No match in the guide right now. It is checked again every 30 minutes, and games show up as the guide fills in (a day or two ahead).</div>');
  if (replays.length) {
    out.push(`<details class="rec-pv-more"><summary>${replays.length} replay${replays.length === 1 ? '' : 's'} it would skip</summary>${
      replays.map(e => row(e, 'dim')).join('')}</details>`);
  }
  if (d.near.length) {
    out.push(`<details class="rec-pv-more"><summary>${d.near.length} near miss${d.near.length === 1 ? '' : 'es'}, and why not</summary>${
      d.near.map(n => `<div class="rec-pv-row dim"><div class="rec-pv-when">${escHtml(_recWhen(n.airing))}</div>
        <div class="rec-pv-what">${escHtml(n.airing.title)} <span class="rec-chan">${escHtml(n.airing.channel)}</span>
        <div class="mych-meta"><span>${escHtml(n.reason)}</span></div></div></div>`).join('')}</details>`);
  }
  return out.join('');
}

async function recSaveEditor() {
  const e = rec.edit;
  if (!e || rec.busy) return;
  const b = e.body;
  if (b.kind === 'keywords' && !(b.terms || []).length) { toast('Type at least one word to look for', 'warning'); return; }
  rec.busy = true;
  try {
    if (e.id) {
      const patch = { ...b };
      delete patch.kind; delete patch.league; delete patch.team;
      await api(`/api/live/recording-rules/${e.id}`, { method: 'PATCH', body: patch });
    } else {
      await api('/api/live/recording-rules', { method: 'POST', body: b });
    }
    closeModal('modal-rec-rule');
    _recStatus('ok', b.mode === 'record' ? 'Saved. Its recordings are booked within a minute.' : 'Saved (preview: nothing is booked).');
    await loadRecordings();
  } catch (err) {
    if (!err.sessionExpired) toast(err.message, 'warning', 6000);
  } finally { rec.busy = false; }
}

async function recRemoveRule() {
  const e = rec.edit;
  const r = e && rec.rules.rules.find(x => x.id === e.id);
  if (!r) return;
  if (!confirm(`Stop following ${r.team_name || r.name}?\n\nIts upcoming recordings are cancelled. What it already recorded stays in Jellyfin.`)) return;
  try {
    await api(`/api/live/recording-rules/${r.id}`, { method: 'DELETE' });
    closeModal('modal-rec-rule');
    _recStatus('ok', `Removed ${r.team_name || r.name}.`);
    await loadRecordings();
  } catch (err) {
    if (!err.sessionExpired) toast(err.message, 'warning', 6000);
  }
}

// ── Recorded ───────────────────────────────────────────────────────────────

function recRenderHistory() {
  const el = document.getElementById('rec-panel-history');
  const items = rec.history?.items;
  if (!items) { el.innerHTML = '<div class="card rec-card"><div class="mych-none">Loading…</div></div>'; return; }
  if (!items.length) {
    el.innerHTML = '<div class="card rec-card"><div class="mych-none">Nothing recorded by your teams or rules yet. Recordings themselves are in Jellyfin: Live TV → Recordings.</div></div>';
    return;
  }
  el.innerHTML = `<div class="card rec-card rec-list">${items.map(b => {
    const restore = b.state === 'skipped' && rec.rules?.can_record
      ? `<button class="btn btn-secondary btn-sm rec-restore" data-id="${b.id}">Bring back</button>` : '';
    return `<div class="mych-row rec-row"><span class="rec-time">${escHtml(_recWhen(b))}</span>
      <span class="mych-text"><span class="mych-name">${escHtml(b.title || '')}</span>
      <span class="mych-meta"><span class="rec-chan">${escHtml(b.channel || '')}</span>${b.rule ? `<span class="via">${escHtml(b.rule)}</span>` : ''}<span>${escHtml(b.reason || '')}</span></span></span>
      ${_recPill(b.state)}${restore}</div>`;
  }).join('')}</div><p class="rec-foot">Watch them in Jellyfin: Live TV → Recordings.</p>`;
}

async function recRestore(id) {
  try {
    await api(`/api/live/recordings/${id}/restore`, { method: 'POST' });
    _recStatus('ok', 'Back on the list: booked at the next look at the guide.');
    rec.history = await api('/api/live/recordings/history');
    await loadRecordings();
  } catch (e) {
    if (!e.sessionExpired) toast(e.message, 'warning', 6000);
  }
}

function _recStatus(kind, text) {
  const el = document.getElementById('rec-status');
  if (!el) return;
  el.className = 'mych-status' + (kind ? ' ' + kind : '');
  el.textContent = text || '';
}

// One listener per panel: buttons carry their data.
document.addEventListener('DOMContentLoaded', () => {
  const page = document.getElementById('page-recordings');
  if (!page) return;
  page.addEventListener('click', ev => {
    const t = ev.target;
    const skip = t.closest('.rec-skip'); if (skip) { recSkip(Number(skip.dataset.id)); return; }
    const edit = t.closest('.rec-edit'); if (edit) { recOpenEditor(Number(edit.dataset.id)); return; }
    const restore = t.closest('.rec-restore'); if (restore) { recRestore(Number(restore.dataset.id)); return; }
    const lg = t.closest('[data-league]'); if (lg) { rec.league = rec.league === lg.dataset.league ? null : lg.dataset.league; rec.find = ''; recRenderTeams(); return; }
    const team = t.closest('[data-team]'); if (team) { recFollowTeam(rec.league, team.dataset.team); return; }
    const series = t.closest('[data-series]'); if (series) { recFollowTeam(series.dataset.series, null); return; }
    if (t.closest('#rec-new-rule')) recOpenEditor(null);
  });
  page.addEventListener('input', ev => { if (ev.target.id === 'rec-find') recFindTeam(ev.target.value); });
  page.addEventListener('change', ev => {
    const m = ev.target.closest('.rec-mode');
    if (m) recSetMode(Number(m.dataset.id), m.checked);
  });
  const ed = document.getElementById('modal-rec-rule');
  ed?.addEventListener('input', ev => { const el = ev.target.closest('.rec-in'); if (el && el.type !== 'checkbox' && el.tagName !== 'SELECT') recEditorInput(el); });
  ed?.addEventListener('change', ev => { const el = ev.target.closest('.rec-in'); if (el && (el.type === 'checkbox' || el.tagName === 'SELECT')) recEditorInput(el); });
  ed?.addEventListener('click', ev => {
    const ha = ev.target.closest('.rec-ha');
    if (ha && rec.edit) {
      rec.edit.body.home_away = ha.dataset.v;
      ed.querySelectorAll('.rec-ha').forEach(x => x.classList.toggle('active', x === ha));
      recPreview();
    }
  });
});

// ── Pictures for recordings (admins: Sources → Live TV → More) ─────────────
// GET/PUT /api/live/recordings/artwork (services/recording_art).

const REC_ART_SOURCES = {
  matchup: 'game cards', league: 'league cards', tmdb: 'show and film pictures',
  guide: 'guide pictures', frame: 'frames from the recording', title: 'title cards',
};

function _recArtText(d) {
  if (!d.enabled) return 'Off. Recordings keep whatever picture Jellyfin has.';
  if (d.blocked) return d.blocked;
  const set = Object.values(d.sources || {}).reduce((a, b) => a + b, 0);
  const parts = Object.entries(d.sources || {}).filter(([, n]) => n)
    .map(([k, n]) => `${n} ${REC_ART_SOURCES[k] || k}`);
  let text = set ? `${set} recording${set === 1 ? '' : 's'} with a picture from Tentacle: ${parts.join(', ')}.`
    : 'On. Recordings get a picture about 10 minutes after they finish.';
  const own = (d.counts || {}).theirs || 0, left = (d.counts || {}).left || 0;
  if (own) text += ` ${own} had a picture already and keep it.`;
  if (left) text += ` ${left} changed or removed by someone, left as they are.`;
  if (d.note) text += ' ' + d.note;
  return text;
}

async function recArtLoad() {
  const btn = document.getElementById('rec-art-toggle');
  const el = document.getElementById('rec-art-status');
  if (!btn || !el) return;
  try {
    const d = await api('/api/live/recordings/artwork');
    _setLiveToggle(btn, d.enabled);
    el.textContent = _recArtText(d);
  } catch (e) {
    el.textContent = 'Couldn’t read this: ' + (e.message || e);
  }
}

async function recArtToggle() {
  const btn = document.getElementById('rec-art-toggle');
  const el = document.getElementById('rec-art-status');
  if (!btn || btn.disabled) return;
  const on = !btn.classList.contains('on');
  btn.disabled = true;
  _setLiveToggle(btn, on);
  try {
    const d = await api('/api/live/recordings/artwork', { method: 'PUT', body: JSON.stringify({ enabled: on }) });
    _setLiveToggle(btn, d.enabled);
    el.textContent = on ? 'On. Recordings get their pictures over the next minutes.'
      : 'Off. Tentacle’s pictures are being removed.';
  } catch (e) {
    _setLiveToggle(btn, !on);
    el.textContent = 'Not saved: ' + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('rec-art-toggle')?.addEventListener('click', recArtToggle);
});
