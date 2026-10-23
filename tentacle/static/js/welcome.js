// Tentacle - the welcome at someone's first sign-in (Roadmap 1e).
//
// Five short steps, shown once, every one skippable: your password (keep it
// or pick your own) with what Tentacle is, your channels (quick picks), your
// home screen, asking for movies and shows, and where to watch. A kid gets
// the same with only what a kid has (their channels are the kids channels,
// no requests). Start or the ✕ ends it for good; Escape or a click beside it
// only hides it until the next visit.
//
// Names and addresses come from the server and are text: everything put
// into innerHTML goes through escHtml.

const welcome = {
  data: null,     // GET /api/welcome
  steps: [],
  at: 0,
};

async function welcomeCheck() {
  try {
    welcome.data = await api('/api/welcome');
  } catch (e) {
    return;
  }
  if (!welcome.data.show) return;
  const d = welcome.data;
  welcome.steps = ['hello'];
  if (d.live_tv) {
    // The quick picks come from My Channels (mychannels.js): the same picks and stars.
    try {
      mych.data = await api('/api/my/channels');
      if (mych.data.available) welcome.steps.push('channels');
    } catch (e) { /* no Live TV to show */ }
  }
  welcome.steps.push('home');
  if (!d.kids) welcome.steps.push('requests');
  welcome.steps.push('watch');
  welcome.at = 0;
  welcome.countsAsked = false;
  mych.onChange = () => { if (welcome.steps[welcome.at] === 'channels') _welcomePicks(); };
  _welcomeShow();
  showModal('modal-welcome');
}

function _welcomeShow() {
  const step = welcome.steps[welcome.at];
  const last = welcome.at === welcome.steps.length - 1;
  document.getElementById('welcome-pips').innerHTML = welcome.steps.map((s, i) =>
    `<li class="${i === welcome.at ? 'active' : i < welcome.at ? 'done' : ''}"><span>${i + 1}</span></li>`).join('');
  document.getElementById('welcome-skip').style.display = last ? 'none' : '';
  document.getElementById('welcome-next').textContent = last ? 'Start' : 'Next';
  const [title, body] = WELCOME_STEPS[step](welcome.data);
  document.getElementById('welcome-title').textContent = title;
  document.getElementById('welcome-body').innerHTML = body;
  if (step === 'channels') _welcomePicks();
  if (step === 'hello' && !welcome.data.counts && !welcome.countsAsked) _welcomeCountsLate();
}

// "You now have access to 12,345 movies, 1,234 TV shows and 1,730 live TV
// channels!", under "Tentacle is your space on Lucas's server". A part that
// is 0 is left out.
function _welcomeCounts(d) {
  const c = d && d.counts;
  if (!c) return '';
  const n = (v, one, many) => `<strong>${Number(v).toLocaleString()} ${v === 1 ? one : many}</strong>`;
  const parts = [];
  if (c.movies > 0) parts.push(n(c.movies, 'movie', 'movies'));
  if (c.shows > 0) parts.push(n(c.shows, 'TV show', 'TV shows'));
  if (c.channels > 0) parts.push(n(c.channels, 'live TV channel', 'live TV channels'));
  if (!parts.length) return '';
  const list = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts[0];
  return `You now have access to ${list}!`;
}

// GET /api/welcome answers at once; the titles may still be being counted.
async function _welcomeCountsLate() {
  welcome.countsAsked = true;
  try {
    const r = await api('/api/welcome/counts');
    if (!r || !r.counts) return;
    welcome.data.counts = r.counts;
    const el = document.getElementById('welcome-counts');
    if (el) el.innerHTML = _welcomeCounts(welcome.data);
  } catch (e) { /* the welcome reads well without them */ }
}

const WELCOME_STEPS = {
  hello: d => [`Welcome, ${d.name}`, `
    <p><strong>Tentacle is your space on ${escHtml(d.server)}.</strong>
      ${d.kids ? 'Here you set up your home screen.' : 'Here you pick your channels, set up your home screen and ask for movies and shows.'}
      You watch in Jellyfin; what you choose here shows up there.</p>
    <p class="welcome-counts" id="welcome-counts">${_welcomeCounts(d)}</p>
    <div class="form-label">Your password</div>
    <label class="fam-radio"><input type="radio" name="welcome-pw" value="keep" checked onchange="_welcomePwMode()"> Keep the one ${escHtml(d.admin)} gave me</label>
    <label class="fam-radio"><input type="radio" name="welcome-pw" value="mine" onchange="_welcomePwMode()"> Pick my own</label>
    <div id="welcome-pw-fields" style="display:none">
      <div class="form-group"><label class="form-label" for="welcome-pw-now">The password you signed in with</label>
        <input class="form-input" id="welcome-pw-now" type="password" autocomplete="current-password"></div>
      <div class="form-group"><label class="form-label" for="welcome-pw-new">Your new password</label>
        <input class="form-input" id="welcome-pw-new" type="password" autocomplete="new-password"></div>
      <div class="form-group"><label class="form-label" for="welcome-pw-again">The new one again</label>
        <input class="form-input" id="welcome-pw-again" type="password" autocomplete="new-password"></div>
      <div class="form-hint">It’s the one password for Tentacle, Jellyfin and the TV app.</div>
    </div>`],

  channels: d => d.kids
    ? ['Your channels', `<p>${escHtml(d.admin)} picked your Live TV channels. They’re your favourites already, so they’re at the top of the guide.</p>
        <p>See them any time under <strong>My Channels</strong>.</p>`]
    : ['Pick your channels', `<p>Tap what you like to watch: those channels become your favourites and come first in the guide.</p>
        <div class="welcome-picks" id="welcome-picks"></div>
        <p class="fam-hint">Change them any time under <strong>My Channels</strong>, where you can also star single channels.</p>`],

  home: d => ['Your home screen', `
    <p>This is what you see first when you open Jellyfin${d.home_rows.length ? ':' : '.'}</p>
    ${d.home_rows.length ? `<ul class="welcome-rows">${d.home_rows.map(r => `<li>${escHtml(r)}</li>`).join('')}</ul>` : ''}
    <p>${d.kids ? 'Ask a grown-up to change it with you' : 'Change the rows, their order and their look'} under <strong>My Home Screen</strong>.</p>`],

  requests: d => ['Asking for movies and shows', `
    <p>Can’t find something? Look it up in <strong>Discover</strong> and press <strong>Request</strong>.</p>
    <p>${d.requests.mode === 'ask'
      ? `${escHtml(d.requests.approver)} says yes first, then it downloads.`
      : 'It starts downloading straight away.'}
      You get a message when it’s ready to watch.</p>`],

  watch: d => ['Where to watch', `
    <div class="welcome-where">
      ${d.jellyfin_address
        ? `<div class="big"><a href="${escHtml(d.jellyfin_address)}" target="_blank" rel="noopener">${escHtml(d.jellyfin_address)}</a></div>`
        : '<div class="big">Jellyfin</div>'}
      <div>On a phone, laptop or computer, open it in the browser. On an iPhone or iPad, use the website too.</div>
    </div>
    ${d.tv_app_address
      ? `<p>Got an Android TV or Google TV? Install the free <strong>Downloader</strong> app, open it and type <strong>${escHtml(d.tv_app_address)}</strong> to get the Tentacle TV app.</p>`
      : ''}
    <p class="fam-hint">Sign in everywhere with the same name and password.</p>`],
};

function _welcomePwMode() {
  const mine = document.querySelector('input[name="welcome-pw"]:checked').value === 'mine';
  document.getElementById('welcome-pw-fields').style.display = mine ? '' : 'none';
  if (mine) setTimeout(() => document.getElementById('welcome-pw-now').focus(), 50);
}

// Quick picks: My Channels' own (mychannels.js), redrawn here after each tap.
// A lineup without types (no quick picks) offers its first channels to star.
function _welcomePicks() {
  const el = document.getElementById('welcome-picks');
  if (!el) return;
  const picks = _mychPicks().slice(0, 8);
  if (picks.length) {
    el.innerHTML = picks.map(p => {
      const label = MYCH_TYPE_LABEL[p.type] || p.type;
      return `<button class="mych-pick${p.on ? ' on' : ''}" data-type="${escHtml(p.type)}" aria-pressed="${p.on}">`
        + `<span class="star" aria-hidden="true">${p.on ? '★' : '☆'}</span> ${escHtml(label)}</button>`;
    }).join('');
  } else {
    el.innerHTML = _mychChoosable().slice(0, 12).map(c =>
      `<button class="mych-pick${c.favourite ? ' on' : ''}" data-key="${escHtml(c.key)}" aria-pressed="${!!c.favourite}">`
      + `<span class="star" aria-hidden="true">${c.favourite ? '★' : '☆'}</span> ${escHtml(c.name)}</button>`).join('')
      || '<span class="fam-hint">No channels to pick from yet.</span>';
  }
  // The pick lights up at the tap; mychannels.js saves it in the background
  // and redraws (through mych.onChange) when it's saved or put back.
  el.querySelectorAll('button[data-type]').forEach(b => {
    b.onclick = () => mychPick(b.dataset.type);
  });
  el.querySelectorAll('button[data-key]').forEach(b => {
    b.onclick = () => _mychStarToggle(b.dataset.key);
  });
}

async function welcomeNext() {
  const step = welcome.steps[welcome.at];
  if (step === 'hello' && !(await _welcomePassword())) return;
  if (welcome.at >= welcome.steps.length - 1) return welcomeFinish();
  welcome.at++;
  _welcomeShow();
}

function welcomeSkip() {
  if (welcome.at >= welcome.steps.length - 1) return welcomeFinish();
  welcome.at++;
  _welcomeShow();
}

// "Pick my own": checked with Jellyfin, then saved. Keep: nothing to do.
async function _welcomePassword() {
  const mine = document.querySelector('input[name="welcome-pw"]:checked');
  if (!mine || mine.value !== 'mine') return true;
  const now = document.getElementById('welcome-pw-now').value;
  const pw = document.getElementById('welcome-pw-new').value;
  const again = document.getElementById('welcome-pw-again').value;
  if (!now) { toast('Type the password you signed in with', 'warning'); return false; }
  if (pw.trim().length < 4) { toast('Use a new password of at least 4 characters', 'warning'); return false; }
  if (pw !== again) { toast('The two new passwords aren’t the same', 'warning'); return false; }
  const btn = document.getElementById('welcome-next');
  btn.disabled = true;
  try {
    await api('/api/me/pw', { method: 'POST', body: { current: now, new: pw } });
    toast('Your new password is saved');
    return true;
  } catch (e) {
    toast(e.message, 'error', 6000);
    return false;
  } finally {
    btn.disabled = false;
  }
}

async function welcomeFinish() {
  closeModal('modal-welcome');
  if (welcome.data) welcome.data.show = false;
  try { await api('/api/welcome/done', { method: 'POST' }); } catch (e) { /* shown again next time */ }
}
