// Tentacle - Settings → Backup & restore (admins; routers/backup.py, #870).
//
// Back up: tick the parts (and whose rows, playlists and lists), download
// the file, or save one on the server ("Back up now"; a snapshot is also
// saved every night, the last 14 kept).
// Restore: a file or a saved backup. Nothing is written until "Restore":
// every change re-asks the server what would happen (added / changed / not
// found, removed with Replace). A provider of the file this Tentacle has no
// provider of that name for is mapped to one of its providers (how much
// matches by name is shown) or skipped.
//
// Names come from providers and people: text, never markup (escHtml).

const bk = {
  info: null,        // GET /api/backup
  exportParts: null, // Set of part ids ticked for a backup
  exportPeople: null,
  src: null,         // {backup} or {snapshot}, and label
  parts: {},         // part id -> {on, replace}
  people: null,      // Set of names, null = everyone
  providers: null,   // file provider name -> provider id here, null = skip
  preview: null,
  seq: 0,
  busy: false,
};

const BK_UNITS = {
  vod_categories: ['category', 'categories'],
  live_channels: ['channel', 'channels'],
  home_rows: ['home', 'homes'],
  playlists: ['playlist', 'playlists'],
  lists: ['list', 'lists'],
  recording_rules: ['rule', 'rules'],
};

function _bkN(n, part) {
  const u = BK_UNITS[part] || ['', ''];
  return `${n} ${n === 1 ? u[0] : u[1]}`;
}

function _bkLabel(id) {
  return ((bk.info?.parts || []).find(p => p.id === id) || {}).label || id;
}

async function bkLoad() {
  const el = document.getElementById('backup-body');
  if (!el) return;
  try {
    bk.info = await api('/api/backup');
  } catch (e) {
    el.innerHTML = `<div class="fam-error">Couldn’t read the backups: ${escHtml(e.message)}</div>`;
    return;
  }
  if (!bk.exportParts) bk.exportParts = new Set(bk.info.parts.map(p => p.id));
  if (!bk.exportPeople) bk.exportPeople = new Set(bk.info.people.map(p => p.name));
  _bkRender();
}

function _bkRender() {
  const el = document.getElementById('backup-body');
  const d = bk.info;
  const parts = d.parts.map(p => {
    const n = p.count == null ? 'everyone’s' : _bkN(p.count, p.id);
    return `<label class="bk-part"><input type="checkbox" ${bk.exportParts.has(p.id) ? 'checked' : ''}
      onchange="bkTickExport('${p.id}', this.checked)"><span>${escHtml(p.label)}
      <small>${n}${p.provider ? ' · per provider' : ''}</small></span></label>`;
  }).join('');
  const people = d.people.length > 1 ? `<div class="form-label">Whose home rows, playlists and lists</div>
    <div class="bk-people">${d.people.map(u => `<label><input type="checkbox" ${bk.exportPeople.has(u.name) ? 'checked' : ''}
      onchange="bkTickExportPerson(${escHtml(JSON.stringify(u.name))}, this.checked)"> ${escHtml(u.name)}</label>`).join('')}</div>` : '';
  const snaps = d.snapshots.length ? `<div class="bk-snaps">${d.snapshots.map(_bkSnapRow).join('')}</div>`
    : '<div class="fam-hint">None yet. The first is saved tonight at 02:30, or now with “Back up now”.</div>';
  el.innerHTML = `
    <div class="bk-block">
      <div class="section-title">Back up</div>
      <div class="bk-parts">${parts}</div>
      ${people}
      <div class="bk-actions">
        <button class="btn btn-primary" onclick="bkDownload()" ${bk.exportParts.size ? '' : 'disabled'}>Download file</button>
        <button class="btn btn-secondary" id="bk-now" onclick="bkNow()">Back up now</button>
      </div>
    </div>
    <div class="bk-block">
      <div class="section-title">Saved backups</div>
      <div class="fam-hint">Everything above, saved every night at 02:30 next to Tentacle’s database; the last ${d.keep} are kept.</div>
      ${snaps}
    </div>
    <div class="bk-block">
      <div class="section-title">Restore</div>
      <div class="bk-actions">
        <button class="btn btn-secondary" onclick="document.getElementById('bk-file').click()">Restore from a file…</button>
        <span class="fam-hint" style="margin:0">or “Restore…” on a saved backup. You see what changes first.</span>
        <input type="file" id="bk-file" accept=".json,application/json" style="display:none" onchange="bkOpenFile(this)">
      </div>
      <div id="bk-restore"></div>
    </div>`;
  if (bk.src) _bkRenderRestore();
}

function _bkWhen(iso) {
  const t = iso ? new Date(iso) : null;
  return t && !isNaN(t) ? t.toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
}

function _bkWhat(s) {
  return Object.entries(s.counts || {}).filter(([, n]) => n).map(([p, n]) => _bkN(n, p)).join(' · ') || 'nothing set up';
}

function _bkSnapRow(s) {
  const kind = s.name.endsWith('-before-restore.json') ? 'before a restore'
    : s.name.endsWith('-now.json') ? 'Back up now' : 'nightly';
  const name = escHtml(s.name);
  return `<div class="bk-snap">
    <div class="bk-snap-when">${escHtml(_bkWhen(s.made_at))}<div class="bk-snap-what" style="font-weight:400">${kind}</div></div>
    <div class="bk-snap-what">${s.problem ? escHtml(s.problem) : escHtml(_bkWhat(s))}</div>
    <a class="btn btn-secondary btn-sm" href="/api/backup/snapshots/${encodeURIComponent(s.name)}" download>Download</a>
    <button class="btn btn-secondary btn-sm" onclick="bkOpenSnapshot('${name}')" ${s.problem ? 'disabled' : ''}>Restore…</button>
  </div>`;
}

function bkTickExport(id, on) {
  on ? bk.exportParts.add(id) : bk.exportParts.delete(id);
  _bkRender();
}

function bkTickExportPerson(name, on) {
  on ? bk.exportPeople.add(name) : bk.exportPeople.delete(name);
}

function bkDownload() {
  const q = new URLSearchParams({ parts: [...bk.exportParts].join(',') });
  if (bk.info.people.length > 1 && bk.exportPeople.size < bk.info.people.length) q.set('people', [...bk.exportPeople].join(','));
  window.location.href = `/api/backup/export?${q}`;
}

async function bkNow() {
  const b = document.getElementById('bk-now');
  if (b) { b.disabled = true; b.textContent = 'Saving…'; }
  try {
    const s = await api('/api/backup/snapshots', { method: 'POST' });
    toast(`Saved: ${_bkWhat(s)}`);
  } catch (e) {
    toast(`Couldn’t save a backup: ${e.message}`, 'error');
  }
  await bkLoad();
}

// ── Restore ──────────────────────────────────────────────────────────────

function bkOpenFile(input) {
  const f = input.files && input.files[0];
  input.value = '';
  if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    let data;
    try { data = JSON.parse(r.result); } catch (e) { toast('That file isn’t a Tentacle backup', 'error'); return; }
    _bkStart({ backup: data }, f.name);
  };
  r.readAsText(f);
}

function bkOpenSnapshot(name) {
  _bkStart({ snapshot: name }, null);
}

function _bkStart(src, label) {
  bk.src = { ...src, label };
  bk.parts = {};
  bk.people = null;
  bk.providers = null;
  bk.preview = null;
  _bkRenderRestore();
  bkPreview();
}

function bkCancel() {
  bk.src = null;
  bk.preview = null;
  const el = document.getElementById('bk-restore');
  if (el) el.innerHTML = '';
}

function _bkBody(forRestore) {
  const body = { ...bk.src };
  delete body.label;
  const fileParts = bk.preview?.file?.parts;
  if (fileParts) {
    // the preview shows every part; a restore does the ticked ones
    body.parts = forRestore ? fileParts.filter(p => bk.parts[p]?.on) : fileParts;
    body.replace = fileParts.filter(p => bk.parts[p]?.replace);
  }
  if (bk.providers) body.providers = bk.providers;
  if (bk.people) body.people = [...bk.people];
  return body;
}

async function bkPreview() {
  const seq = ++bk.seq;
  try {
    const p = await api('/api/backup/preview', { method: 'POST', body: _bkBody(false) });
    if (seq !== bk.seq) return;
    bk.preview = p;
    for (const id of p.file.parts) bk.parts[id] = bk.parts[id] || { on: true, replace: false };
    if (!bk.providers) {
      bk.providers = {};
      for (const c of p.providers) bk.providers[c.name] = c.target;
    }
  } catch (e) {
    if (seq !== bk.seq) return;
    bk.preview = { error: e.message };
  }
  _bkRenderRestore();
}

function _bkCounts(rep) {
  if (!rep) return '';
  const bits = [];
  if (rep.added) bits.push(`<span class="bk-n add">+${rep.added} added</span>`);
  if (rep.changed) bits.push(`<span class="bk-n chg">${rep.changed} changed</span>`);
  if (rep.removed) bits.push(`<span class="bk-n del">−${rep.removed} removed</span>`);
  if (rep.not_found.length) bits.push(`<span class="bk-n miss">${rep.not_found.length} not found here</span>`);
  if (!bits.length) bits.push('<span class="bk-n">nothing to change</span>');
  if (rep.unchanged && (rep.added || rep.changed || rep.removed)) bits.push(`<span class="bk-n">${rep.unchanged} already so</span>`);
  const more = [];
  if (rep.not_found.length) more.push(`<li>Not found here: ${rep.not_found.slice(0, 40).map(escHtml).join(', ')}${rep.not_found.length > 40 ? ` and ${rep.not_found.length - 40} more` : ''}</li>`);
  for (const n of rep.notes) more.push(`<li>${escHtml(n)}</li>`);
  for (const n of rep.detail.slice(0, 20)) more.push(`<li>${escHtml(n)}</li>`);
  return bits.join('') + (more.length ? `<details><summary>Details</summary><ul>${more.join('')}</ul></details>` : '');
}

function _bkMatchText(c) {
  if (!c) return '';
  const bits = [];
  if (c.vod_total) bits.push(`${c.vod_matched} of ${c.vod_total} categories`);
  if (c.groups_total) bits.push(`${c.groups_matched} of ${c.groups_total} groups`);
  if (c.channels_total) bits.push(`${c.channels_matched} of ${c.channels_total} channels`);
  return bits.length ? `${bits.join(' · ')} match by name` : 'nothing of it is in the file';
}

function _bkRenderRestore() {
  const el = document.getElementById('bk-restore');
  if (!el || !bk.src) return;
  const p = bk.preview;
  const title = bk.src.label ? `“${escHtml(bk.src.label)}”` : 'the saved backup';
  if (!p) {
    el.innerHTML = `<div class="bk-restore"><h4>Restore ${title}</h4><div class="loading-state"><div class="spinner"></div> Reading&hellip;</div></div>`;
    return;
  }
  if (p.error) {
    el.innerHTML = `<div class="bk-restore"><h4>Restore ${title}</h4><div class="fam-error">${escHtml(p.error)}</div>
      <div class="bk-actions" style="margin-top:10px"><button class="btn btn-secondary btn-sm" onclick="bkCancel()">Close</button></div></div>`;
    return;
  }
  const f = p.file;
  const here = (bk.info?.providers || []);
  const providerParts = f.parts.some(x => x === 'vod_categories' || x === 'live_channels');
  let provs = '';
  if (providerParts && p.providers.length) {
    provs = `<div class="bk-sub">Providers</div>
      <div class="fam-hint" style="margin-top:0">VOD categories and Live TV channels belong to a provider. Pick where each one’s go, or skip it.</div>
      ${p.providers.map(c => {
        const opts = here.map(h => `<option value="${h.id}" ${c.target === h.id ? 'selected' : ''}>${escHtml(h.name)}${c.same_name === h.id ? ' (same name)' : ''}</option>`).join('');
        const sel = c.candidates.find(x => x.id === c.target);
        const text = !here.length ? 'This Tentacle has no provider yet: add it in Sources first, or skip it.'
          : c.target == null ? (c.same_name == null ? 'No provider of this name here: pick one or skip it.' : 'Skipped.')
          : _bkMatchText(sel);
        return `<div class="bk-prov"><div><strong>${escHtml(c.name)}</strong></div>
          <select aria-label="Where ${escHtml(c.name)} goes" onchange="bkMapProvider(${escHtml(JSON.stringify(c.name))}, this.value)">
            <option value="" ${c.target == null ? 'selected' : ''}>Skip</option>${opts}</select>
          <div class="bk-result">${text}</div></div>`;
      }).join('')}`;
  }
  const rows = f.parts.map(id => {
    const st = bk.parts[id] || { on: true, replace: false };
    const rep = p.parts[id];
    return `<div class="bk-row ${st.on ? '' : 'off'}">
      <label class="bk-part"><input type="checkbox" ${st.on ? 'checked' : ''} onchange="bkTickPart('${id}', this.checked)">
        <span>${escHtml(_bkLabel(id))}<small>${f.counts[id] != null ? _bkN(f.counts[id], id) + ' in the file' : ''}</small></span></label>
      <select aria-label="How to restore ${escHtml(_bkLabel(id))}" onchange="bkReplace('${id}', this.value === 'replace')" ${st.on ? '' : 'disabled'}>
        <option value="merge" ${st.replace ? '' : 'selected'}>Add &amp; update</option>
        <option value="replace" ${st.replace ? 'selected' : ''}>Replace</option>
      </select>
      <div class="bk-result">${_bkCounts(rep)}</div></div>`;
  }).join('');
  const peopleParts = f.parts.some(x => ['home_rows', 'playlists', 'lists'].includes(x) && bk.parts[x]?.on);
  const people = peopleParts && f.people.length > 1 ? `<div class="bk-sub">Whose home rows, playlists and lists</div>
    <div class="bk-people">${f.people.map(n => `<label><input type="checkbox" ${!bk.people || bk.people.has(n) ? 'checked' : ''}
      onchange="bkTickPerson(${escHtml(JSON.stringify(n))}, this.checked)"> ${escHtml(n)}</label>`).join('')}</div>` : '';
  const leftOut = (p.left_out || []).length ? `<div class="fam-hint">Left out of the file when it was made: ${p.left_out.map(escHtml).join('; ')}.</div>` : '';
  const chosen = f.parts.filter(id => bk.parts[id]?.on);
  const work = chosen.some(id => { const r = p.parts[id]; return r && (r.added || r.changed || r.removed); });
  const replacing = chosen.filter(id => bk.parts[id]?.replace && p.parts[id]?.removed);
  el.innerHTML = `<div class="bk-restore">
    <h4>Restore ${title}</h4>
    <div class="fam-hint" style="margin-top:0">Made ${escHtml(_bkWhen(f.made_at))}${f.tentacle ? ` by Tentacle ${escHtml(f.tentacle)}` : ''}. Nothing changes until you press Restore. “Add &amp; update” never removes anything; “Replace” also takes away what the file doesn’t have.</div>
    ${provs}
    <div class="bk-sub">What to restore</div>
    ${rows}
    ${people}
    ${leftOut}
    <div class="bk-actions" style="margin-top:14px">
      <button class="btn ${replacing.length ? 'btn-danger' : 'btn-primary'}" id="bk-go" onclick="bkRestore()" ${work && !bk.busy ? '' : 'disabled'}>${bk.busy ? 'Restoring…' : 'Restore'}</button>
      <button class="btn btn-secondary" onclick="bkCancel()">Cancel</button>
      <span class="fam-hint" style="margin:0">${work ? 'Your setup as it is now is saved first, so a restore can be undone.' : 'Nothing to restore with these choices.'}</span>
    </div>
  </div>`;
}

function bkTickPart(id, on) {
  bk.parts[id] = { ...(bk.parts[id] || {}), on };
  _bkRenderRestore();
}

function bkReplace(id, replace) {
  bk.parts[id] = { ...(bk.parts[id] || { on: true }), replace };
  bkPreview();
}

function bkMapProvider(name, value) {
  bk.providers = { ...(bk.providers || {}), [name]: value === '' ? null : Number(value) };
  bkPreview();
}

function bkTickPerson(name, on) {
  const all = bk.preview?.file?.people || [];
  const s = bk.people ? new Set(bk.people) : new Set(all);
  on ? s.add(name) : s.delete(name);
  bk.people = s.size === all.length ? null : s;
  bkPreview();
}

async function bkRestore() {
  const p = bk.preview;
  if (!p || bk.busy) return;
  const removing = Object.entries(p.parts).filter(([id, r]) => bk.parts[id]?.on && bk.parts[id]?.replace && r.removed)
    .map(([id, r]) => `${_bkN(r.removed, id)} (${_bkLabel(id)})`);
  if (removing.length && !confirm(`Replace removes ${removing.join(', ')} that the file doesn’t have. Restore?`)) return;
  bk.busy = true;
  _bkRenderRestore();
  try {
    const out = await api('/api/backup/restore', { method: 'POST', body: _bkBody(true) });
    const sum = Object.entries(out.parts).map(([id, r]) => `${_bkLabel(id)}: +${r.added} ~${r.changed}${r.removed ? ` −${r.removed}` : ''}`).join('; ');
    toast(`Restored. ${sum}`, 'success', 8000);
    bk.src = null;
    bk.preview = null;
    await bkLoad();
    const el = document.getElementById('bk-restore');
    if (el) {
      el.innerHTML = `<div class="fam-note" style="background:var(--green-dim);border:1px solid rgba(74,222,128,.3);margin-top:12px"><span><strong>Restored.</strong> ${escHtml(sum)}.
        Playlists and home rows finish building in Jellyfin in the background. Your setup from before is the saved backup “before a restore” at the top of the list.</span></div>`;
    }
  } catch (e) {
    toast(`Restore failed: ${e.message}`, 'error', 8000);
  } finally {
    bk.busy = false;
    if (bk.src) _bkRenderRestore();
  }
}
