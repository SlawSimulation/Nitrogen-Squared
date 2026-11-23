// SPDX-License-Identifier: GPL-3.0-or-later
// Original Soundtrack page: ten CD-DA slots, previews, replacement and export.
// File names and package details are user data and are always escaped.
import './soundtrack.css';
import { esc, icon, confirm, toast, errText } from '../../ui.js';
import { store } from '../../store.js';
import { request } from '../../bridge.js';

const STATE = { original: ['', 'Original'], moved: ['warn', 'Moved'], replacement: ['ok', 'Replacement'] };
const time = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const mib = (b) => `${(b / 1048576).toFixed(1)} MiB`;
const FIELDS = [['name', 'Name'], ['id', 'Package id'], ['version', 'Version'], ['author', 'Author'], ['license', 'Licence']];

function row(t, blocked) {
  const [kind, label] = STATE[t.state];
  const slot = String(t.slot).padStart(2, '0');
  return `<li class="st-row glass" data-slot="${t.slot}">
    <span class="st-num num">${slot}</span>
    <span class="st-main"><strong class="st-label clip" title="${esc(t.label)}">${esc(t.label)}</strong>
      <span class="faint">${time(t.seconds)} · ${esc(t.format)}</span></span>
    <span class="chip ${kind} st-state">${esc(label)}</span>
    <span class="st-actions">
      <button class="btn sm icon-only" data-act="preview" aria-label="Preview track ${slot}" title="Preview">${icon('play')}</button>
      <button class="btn sm icon-only" data-act="up" aria-label="Move track ${slot} up" title="Move up" ${blocked || t.slot === 2 ? 'disabled' : ''}>${icon('arrow-up')}</button>
      <button class="btn sm icon-only" data-act="down" aria-label="Move track ${slot} down" title="Move down" ${blocked || t.slot === 11 ? 'disabled' : ''}>${icon('arrow-down')}</button>
      <button class="btn sm" data-act="replace" ${blocked ? 'disabled' : ''}>${icon('upload')}Replace…</button>
      <button class="btn sm quiet" data-act="restore" ${blocked || t.state === 'original' ? 'disabled' : ''}>${icon('rotate-ccw')}Restore</button>
    </span></li>`;
}

export function render(el, ctx) {
  el.innerHTML = `<div class="page st-page">
    <header class="st-head"><div><h1>Soundtrack</h1><p class="faint">Replace, reorder and preview the game's ten CD audio tracks, then export them as a mod package. Your disc and extracted files are never changed.</p></div>
      <div class="cluster"><button class="btn" id="st-preview-all">${icon('play')}Preview playlist</button><button class="btn" id="st-stop">${icon('square')}Stop preview</button></div></header>
    <div id="st-notice"></div><ol class="st-list" id="st-list" aria-label="Soundtrack slots"></ol>
    <section class="st-export glass"><h2>Export</h2>
      <div class="st-fields">${FIELDS.map(([k, l]) => `<label class="st-field"><span class="faint">${l}</span><input class="input" data-field="${k}" spellcheck="false" autocomplete="off" /></label>`).join('')}</div>
      <p class="faint st-budget" id="st-budget"></p>
      <div class="cluster"><button class="btn primary" id="st-export-install">${icon('package')}Export and install</button><button class="btn" id="st-export">${icon('download')}Export package</button><button class="btn" data-open="soundtrack-exports">${icon('folder-open')}Open exports</button><button class="btn quiet" id="st-reset">${icon('rotate-ccw')}Restore all</button></div>
      <p class="faint">Packages contain only your replacement audio and a playlist; unchanged tracks still play from your own disc. Enable an installed package on the Mods page.</p>
    </section></div>`;
  const list = el.querySelector('#st-list'), notice = el.querySelector('#st-notice');
  let data = null, seen = null, loading = false, edited = false;
  const blocked = () => !store.state || store.state.busy || store.state.recovery;

  function draw() {
    const lock = blocked() || !data || data.problem;
    for (const id of ['#st-export', '#st-export-install', '#st-reset']) el.querySelector(id).disabled = lock || (id !== '#st-reset' && !data?.changed);
    el.querySelector('#st-preview-all').disabled = !data || !!data.problem;
    for (const input of el.querySelectorAll('[data-field]')) input.disabled = lock;
    if (!data) { list.innerHTML = '<li class="faint">Loading soundtrack…</li>'; return; }
    if (data.problem) { notice.innerHTML = `<div class="banner info">${icon('info')}<span>${esc(data.problem)}</span></div>`; list.innerHTML = ''; return; }
    notice.innerHTML = '';
    list.innerHTML = data.slots.map((t) => row(t, blocked())).join('');
    if (!edited) for (const input of el.querySelectorAll('[data-field]')) input.value = data.details[input.dataset.field] ?? '';
    el.querySelector('#st-budget').textContent = `Replacement audio: ${mib(data.replacement_bytes)} of ${mib(data.limit_bytes)} per package · ten tracks, slots 02–11`;
  }

  async function load() {
    if (loading) return;
    loading = true;
    try { data = await request('soundtrack'); }
    catch (e) { data = { problem: errText(e) }; }
    finally { loading = false; }
    draw();
  }
  async function act(command, args = {}, ok = '') {
    try { await store.perform(command, args); if (ok) toast(ok, 'ok'); await load(); }
    catch { /* store.perform already reported the error */ }
  }
  async function saveDetails() {
    if (!edited) return true;
    const details = Object.fromEntries([...el.querySelectorAll('[data-field]')].map((i) => [i.dataset.field, i.value]));
    try { await store.perform('soundtrack_details', { details }); edited = false; return true; } catch { return false; }
  }

  ctx.on((s) => {
    const key = `${s?.soundtrack_revision}:${s?.busy}`;
    if (key !== seen) { seen = key; if (!s?.busy || data) load(); }
    draw();
  });
  el.addEventListener('input', (e) => { if (e.target.dataset.field) edited = true; });
  el.addEventListener('click', async (e) => {
    const button = e.target.closest('button');
    if (!button) return;
    const slot = Number(button.closest('[data-slot]')?.dataset.slot);
    const a = button.dataset.act;
    if (a === 'preview') act('soundtrack_preview', { slot }, `Previewing track ${String(slot).padStart(2, '0')} (first 30 seconds).`);
    else if (a === 'up' || a === 'down') act('soundtrack_move', { slot, offset: a === 'up' ? -1 : 1 });
    else if (a === 'replace') act('choose_wav', { slot });
    else if (a === 'restore') act('soundtrack_restore', { slot });
    else if (button.id === 'st-preview-all') act('soundtrack_preview', { slot: null }, 'Previewing the playlist: 8 seconds of each track.');
    else if (button.id === 'st-stop') act('soundtrack_stop');
    else if (button.dataset.open) store.perform('open', { target: button.dataset.open }).catch(() => {});
    else if (button.id === 'st-reset') {
      if (await confirm({ title: 'Restore all tracks?', body: 'Every slot returns to its original disc track. Imported replacements are discarded from this project.', ok: 'Restore all', danger: true })) act('soundtrack_reset');
    } else if (button.id === 'st-export' || button.id === 'st-export-install') {
      if (await saveDetails()) act('soundtrack_export', { install: button.id === 'st-export-install' });
    }
  });
  seen = `${store.state?.soundtrack_revision}:${store.state?.busy}`;
  draw(); load();
}
