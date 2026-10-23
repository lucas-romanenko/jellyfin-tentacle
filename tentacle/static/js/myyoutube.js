// Tentacle - My YouTube (everyone): my own YouTube channels.
//
// Each person adds the channels they want by pasting a link; only they see
// a channel's videos and its Live TV channel in Jellyfin (another person
// who adds the same channel shares the one copy). Removing one takes it out
// of their Jellyfin; the last person to remove a channel removes it from the
// server. A kids profile sees the channels a grown-up added for them and
// can't change them. Admins see everyone's on Sources → YouTube.
//
// Channel names come from YouTube: they are text, never markup (every row
// goes through escHtml).

const myt = {
  data: null,        // GET /api/my/youtube
  seen: '',
  watch: null,
  busy: false,       // an add or a remove on its way
};

async function loadMyYouTube() {
  try {
    myt.data = await api('/api/my/youtube');
  } catch (e) {
    toast('Your YouTube channels could not be loaded: ' + e.message, 'error');
    return;
  }
  myt.seen = JSON.stringify(myt.data);
  mytRender();
  _mytWatch();
}

function mytRender() {
  const d = myt.data;
  if (!d) return;
  const kids = !!d.kids;
  document.getElementById('myt-add-card').style.display = d.can_add ? '' : 'none';
  const n = d.channels.length;
  document.getElementById('myt-total').textContent = n ? `· ${n} channel${n === 1 ? '' : 's'}` : '';
  document.getElementById('myt-count').textContent = n || '';
  document.getElementById('myt-sub').textContent = kids
    ? 'The YouTube channels a grown-up added for you. Their newest videos are in your library.'
    : 'Paste a link to add a channel. Its newest videos come into your library and you can put it on your home screen. Nobody else sees your channels.';
  const list = document.getElementById('myt-list');
  if (!n) {
    list.innerHTML = `<div class="mych-none">${kids
      ? 'No YouTube channels yet. Ask a grown-up to add some for you.'
      : 'No channels yet. Paste a YouTube link above.'}</div>`;
    return;
  }
  list.innerHTML = d.channels.map(c => {
    const meta = [];
    if (c.loading) meta.push('<span class="ev">Getting its videos… they arrive in a minute or two</span>');
    else meta.push(`<span>${c.videos} video${c.videos === 1 ? '' : 's'} <span class="myt-dim">(the newest ${c.keep_count || c.videos})</span></span>`);
    if (c.live_now) meta.push('<span class="bad">● Live now</span>');
    else if (c.live_enabled) meta.push('<span>Live TV channel</span>');
    if (c.added_by) meta.push(`<span>Added by ${escHtml(c.added_by)}</span>`);
    const avatar = c.avatar_url
      ? `<img class="myt-avatar" src="${escHtml(c.avatar_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.className='myt-avatar none';this.removeAttribute('src')">`
      : '<span class="myt-avatar none"></span>';
    const remove = d.can_add
      ? `<button class="btn btn-secondary btn-sm myt-remove" data-id="${c.id}" aria-label="Remove ${escHtml(c.title)}">Remove</button>`
      : '';
    return `<div class="mych-row myt-row" data-id="${c.id}">${avatar}
      <span class="mych-text"><span class="mych-name" title="${escHtml(c.title)}">${escHtml(c.title)}</span>
      <span class="mych-meta">${meta.join('')}</span></span>${remove}</div>`;
  }).join('');
}

function _mytStatus(kind, text) {
  const el = document.getElementById('myt-status');
  if (!el) return;
  el.className = 'mych-status' + (kind ? ' ' + kind : '');
  el.textContent = text || '';
}

async function mytAdd(ev) {
  if (ev) ev.preventDefault();
  if (myt.busy) return;
  const input = document.getElementById('myt-url');
  const url = input.value.trim();
  if (!url) { toast('Paste a YouTube channel or playlist link', 'warning'); input.focus(); return; }
  const live = document.getElementById('myt-live').checked;
  const btn = document.getElementById('myt-add-btn');
  myt.busy = true;
  btn.disabled = true;
  _mytStatus('busy', 'Looking it up on YouTube…');
  try {
    const d = await api('/api/my/youtube', { method: 'POST', body: { url, live } });
    myt.data = d;
    myt.seen = JSON.stringify(d);
    input.value = '';
    document.getElementById('myt-live').checked = false;
    mytRender();
    _mytStatus('ok', d.added.new
      ? `Added ${d.added.title}. Its newest videos arrive in a minute or two.`
      : `Added ${d.added.title}. Its videos are in your library now.`);
  } catch (e) {
    _mytStatus('', '');
    if (!e.sessionExpired) toast(e.message, 'warning', 6000);
  } finally {
    myt.busy = false;
    btn.disabled = false;
  }
}

async function mytRemove(id) {
  const c = (myt.data?.channels || []).find(x => x.id === id);
  if (!c || myt.busy) return;
  if (!confirm(`Remove "${c.title}"?\n\nIts videos and its home row leave your Jellyfin.`)) return;
  myt.busy = true;
  // At once on the page; put back if the server says no.
  const before = myt.data;
  myt.data = { ...before, channels: before.channels.filter(x => x.id !== id) };
  mytRender();
  _mytStatus('busy', 'Removing…');
  try {
    const d = await api(`/api/my/youtube/${id}`, { method: 'DELETE' });
    myt.data = d;
    myt.seen = JSON.stringify(d);
    mytRender();
    _mytStatus('ok', `Removed ${c.title}.`);
  } catch (e) {
    myt.data = before;
    mytRender();
    _mytStatus('', '');
    if (!e.sessionExpired) toast(`${c.title} wasn't removed: ${e.message}`, 'warning', 6000);
  } finally {
    myt.busy = false;
  }
}

// Live while the page is open: a channel an admin added for me, its videos
// arriving, a live stream starting show within seconds.
function _mytWatch() {
  if (myt.watch) return;
  myt.watch = setInterval(async () => {
    if (state.currentPage !== 'myyoutube') { clearInterval(myt.watch); myt.watch = null; return; }
    if (document.hidden || myt.busy || document.activeElement?.id === 'myt-url' && document.activeElement.value) return;
    try {
      const d = await api('/api/my/youtube');
      const sig = JSON.stringify(d);
      if (sig !== myt.seen && state.currentPage === 'myyoutube' && !myt.busy) { myt.data = d; myt.seen = sig; mytRender(); }
    } catch (e) { /* the next round tries again */ }
  }, 8000);
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('myt-list')?.addEventListener('click', ev => {
    const btn = ev.target.closest('.myt-remove');
    if (btn) mytRemove(Number(btn.dataset.id));
  });
});
