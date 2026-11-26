// Tentacle - Subtitles & skipping (everyone): their own subtitle languages,
// when subtitles show, and what Skip intro / Skip credits do.
//
// Saved at once (PUT /api/watch/prefs, services/watch_prefs.py) and passed
// on to Jellyfin's own per-user subtitle settings, so every app follows;
// open Jellyfin pages and the TV app hear about it through the plugin
// (topic "watch"). Until someone picks their own, the household's starting
// point applies (Server → People → "Subtitles & skipping to start with",
// rendered here too by famRenderWatchDefaults).

const wch = { data: null, busy: false };

async function loadWatching() {
  try {
    wch.data = await api('/api/watch/prefs');
  } catch (e) {
    toast('Your subtitle settings could not be loaded: ' + e.message, 'error');
    return;
  }
  wchRender();
}

function _wchName(code) {
  const l = (wch.data.languages || []).find(x => x.code === code);
  return l ? l.name : code;
}

function wchRender() {
  const d = wch.data;
  if (!d) return;
  document.getElementById('wch-body').style.display = '';
  const langs = d.subtitle_languages || [];
  document.getElementById('wch-langs').innerHTML = langs.map((c, i) =>
    `<span class="wch-lang">${escHtml(_wchName(c))}${i === 0 ? ' <span class="wch-first">your language</span>' : ''}` +
    (langs.length > 1 ? `<button type="button" data-code="${escHtml(c)}" aria-label="Remove ${escHtml(_wchName(c))}" onclick="wchRemoveLanguage(this.dataset.code)">×</button>` : '') +
    '</span>').join('');
  const add = document.getElementById('wch-add-lang');
  add.innerHTML = '<option value="">+ Add a language</option>' + (d.languages || [])
    .filter(l => !langs.includes(l.code))
    .map(l => `<option value="${escHtml(l.code)}">${escHtml(l.name)}${l.own_name && l.own_name !== l.name ? ' · ' + escHtml(l.own_name) : ''}</option>`).join('');
  for (const [name, value] of [['wch-mode', d.subtitle_mode], ['wch-intro', d.skip_intro], ['wch-credits', d.skip_credits]]) {
    document.querySelectorAll(`input[name="${name}"]`).forEach(r => { r.checked = r.value === value; });
  }
  const own = d.own || {};
  const anyOwn = Object.values(own).some(Boolean);
  document.getElementById('wch-foot').innerHTML = anyOwn
    ? `<span>These are your own choices.</span><button class="btn btn-secondary btn-sm" onclick="wchReset()">Use the household&rsquo;s instead</button>`
    : `<span>You&rsquo;re using what the household starts everyone with${d.kid ? ' (kids)' : ''}. Change anything and it&rsquo;s yours.</span>`;
}

function _wchStatus(kind, text) {
  const el = document.getElementById('wch-status');
  if (!el) return;
  el.className = 'mych-status' + (kind ? ' ' + kind : '');
  el.textContent = text || '';
}

async function wchSave(change) {
  if (wch.busy) return;
  wch.busy = true;
  _wchStatus('busy', 'Saving…');
  try {
    wch.data = await api('/api/watch/prefs', { method: 'PUT', body: JSON.stringify(change) });
    _wchStatus('ok', 'Saved. Your devices pick it up within a few seconds.');
  } catch (e) {
    _wchStatus('', '');
    toast(e.message, 'error');
  }
  wch.busy = false;
  wchRender();
}

function wchAddLanguage(code) {
  if (!code) return;
  wchSave({ subtitle_languages: [...(wch.data.subtitle_languages || []), code] });
}

function wchRemoveLanguage(code) {
  const langs = (wch.data.subtitle_languages || []).filter(c => c !== code);
  if (langs.length) wchSave({ subtitle_languages: langs });
}

async function wchReset() {
  try {
    wch.data = await api('/api/watch/prefs/reset', { method: 'POST' });
    _wchStatus('ok', 'Back to the household’s settings.');
  } catch (e) {
    toast(e.message, 'error');
  }
  wchRender();
}

// ── Server → People: what everyone starts with (admins) ──────────────────

const WCH_CHOICES = {
  subtitle_mode: [['off', 'Off until turned on'], ['always', 'Always'], ['foreign', 'When the sound isn’t in their language']],
  skip_intro: [['button', 'Show a Skip intro button'], ['auto', 'Skip by itself'], ['off', 'Off']],
  skip_credits: [['button', 'Show a Skip credits button'], ['auto', 'Skip by itself'], ['off', 'Off']],
};

async function famRenderWatchDefaults() {
  const box = document.getElementById('fam-watch-defaults');
  if (!box) return;
  let d;
  try {
    d = await api('/api/watch/defaults');
  } catch (e) {
    box.innerHTML = `<p class="fam-hint">Couldn't load them: ${escHtml(e.message)}</p>`;
    return;
  }
  const sel = (who, field, value) => `<select class="form-input" data-who="${who}" data-field="${field}" onchange="famSaveWatchDefault(this)" aria-label="${field.replace('_', ' ')}">` +
    WCH_CHOICES[field].map(([v, t]) => `<option value="${v}"${v === value ? ' selected' : ''}>${escHtml(t)}</option>`).join('') + '</select>';
  const lang = (who, value) => `<select class="form-input" data-who="${who}" data-field="subtitle_languages" onchange="famSaveWatchDefault(this)" aria-label="Subtitle language">` +
    d.languages.map(l => `<option value="${l.code}"${l.code === value ? ' selected' : ''}>${escHtml(l.name)}</option>`).join('') + '</select>';
  box.innerHTML = [['grownups', 'Grown-ups'], ['kids', 'Kids']].map(([who, label]) => {
    const p = d[who];
    return `<div class="wch-col"><div class="wch-who">${label}</div>
      <div class="form-group"><label class="form-label">Subtitles in</label>${lang(who, p.subtitle_languages[0])}</div>
      <div class="form-group"><label class="form-label">Subtitles show</label>${sel(who, 'subtitle_mode', p.subtitle_mode)}</div>
      <div class="form-group"><label class="form-label">Intros</label>${sel(who, 'skip_intro', p.skip_intro)}</div>
      <div class="form-group"><label class="form-label">End credits</label>${sel(who, 'skip_credits', p.skip_credits)}</div></div>`;
  }).join('');
}

async function famSaveWatchDefault(el) {
  const field = el.dataset.field;
  const value = field === 'subtitle_languages' ? [el.value] : el.value;
  try {
    await api('/api/watch/defaults', { method: 'PUT', body: JSON.stringify({ [el.dataset.who]: { [field]: value } }) });
    toast('Saved. Everyone who hasn’t picked their own follows it.');
  } catch (e) {
    toast(e.message, 'error');
    famRenderWatchDefaults();
  }
}

// Someone's choices changed (theirs on another device, or the household's).
if (typeof live !== 'undefined') {
  live.handlers.watch = () => {
    if (state.currentPage === 'watching' && !liveOwnChange() && !wch.busy) loadWatching();
    if (state.currentPage === 'people' && !liveOwnChange() && typeof _isAdmin === 'function' && _isAdmin()) famRenderWatchDefaults();
  };
}
