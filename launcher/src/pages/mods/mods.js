// SPDX-License-Identifier: GPL-3.0-or-later
// Original Mods page: installed .psxmod packages from the durable .local/mods store.
// Package text is untrusted data and is always escaped.
import './mods.css';
import { esc, icon, toggle, confirm, toast, errText } from '../../ui.js';
import { store } from '../../store.js';
import { request } from '../../bridge.js';

const STATUS = {
  ready: ['ok', 'circle-check', 'Compatible'],
  unchecked: ['warn', 'circle-alert', 'Not checked yet'],
  'missing-plugins': ['danger', 'circle-x', 'Missing plugins'],
  invalid: ['danger', 'circle-x', 'Invalid package'],
  'other-game': ['danger', 'circle-x', 'Different game'],
  'needs-files': ['danger', 'circle-x', 'Needs files'],
};

function card(p, blocked) {
  const [kind, mark, label] = STATUS[p.compatibility.state] || STATUS.unchecked;
  const canEnable = p.enabled || ['ready', 'unchecked'].includes(p.compatibility.state);
  const plugins = p.required_plugins.length
    ? p.required_plugins.map((id) => `<span class="chip ${p.missing_plugins.includes(id) ? 'danger' : ''}">${esc(id)}</span>`).join('')
    : '<span class="faint">None</span>';
  return `<article class="mod glass" data-id="${esc(p.id)}">
    <header class="mod-head">
      <div class="mod-title"><h2>${esc(p.name)}</h2><span class="faint mod-version">${esc(p.version)}</span></div>
      ${toggle('mod-' + p.id, p.enabled, { label: `Enable ${p.name}`, disabled: blocked || !canEnable })}
    </header>
    ${p.description ? `<p class="mod-desc">${esc(p.description)}</p>` : ''}
    <dl class="mod-facts">
      <dt>Author</dt><dd>${esc(p.author) || '<span class="faint">Not stated</span>'}</dd>
      <dt>Licence</dt><dd>${esc(p.license) || '<span class="faint">Not stated</span>'}</dd>
      <dt>Package</dt><dd class="selectable mono">${esc(p.id)}</dd>
      <dt>Required plugins</dt><dd class="mod-plugins">${plugins}</dd>
    </dl>
    <footer class="mod-foot">
      <span class="chip ${kind} mod-status" title="${esc(p.compatibility.detail)}">${icon(mark)}${esc(label)}</span>
      <span class="faint mod-detail">${esc(p.compatibility.detail)}</span>
      <button class="btn sm quiet" data-remove="${esc(p.id)}" ${blocked ? 'disabled' : ''}>${icon('trash-2')}Remove</button>
    </footer>
  </article>`;
}

export function render(el, ctx) {
  el.innerHTML = `<div class="page mods-page">
    <header class="mods-head"><div><h1>Mods</h1><p class="faint">Packages are kept in your .local folder and survive game rebuilds and launcher updates.</p></div>
      <div class="cluster"><button class="btn primary" id="mod-install">${icon('upload')}Install package…</button><button class="btn" data-open="mods">${icon('folder-open')}Open mods folder</button></div></header>
    <div id="mods-notice"></div><div class="mods-list" id="mods-list" aria-live="polite"></div></div>`;
  const list = el.querySelector('#mods-list'), notice = el.querySelector('#mods-notice');
  let listing = null, seen = null, loading = false;
  const blocked = () => !store.state || store.state.busy || store.state.recovery;

  function draw() {
    el.querySelector('#mod-install').disabled = blocked();
    if (!listing) { list.innerHTML = '<p class="faint">Loading packages…</p>'; return; }
    notice.innerHTML = listing.problem
      ? `<div class="banner danger">${icon('triangle-alert')}<span>${esc(listing.problem)}</span></div>`
      : !listing.checked && listing.packages.length
        ? `<div class="banner info">${icon('info')}<span>Prepare the game once so each package can be checked against your game build.</span></div>` : '';
    list.innerHTML = listing.packages.length
      ? listing.packages.map((p) => card(p, blocked())).join('')
      : `<div class="mods-empty glass">${icon('file-archive')}<h2>No mods installed</h2><p class="faint">Install a .psxmod package to add it here. Mods are off until you enable them, and the game never needs your disc changed.</p></div>`;
  }

  async function load() {
    if (loading) return;
    loading = true;
    try { listing = await request('mods'); }
    catch (e) { listing = { packages: [], checked: false, problem: errText(e) }; }
    finally { loading = false; }
    draw();
  }

  ctx.on((s) => {
    const key = `${s?.mods_revision}:${s?.busy}`;
    if (key !== seen) { seen = key; if (!s?.busy || listing) load(); }
    draw();
  });

  el.addEventListener('change', async (e) => {
    const card = e.target.closest('.mod');
    if (!card || e.target.type !== 'checkbox') return;
    try { await store.perform('mod_enable', { id: card.dataset.id, enabled: e.target.checked }); toast(e.target.checked ? 'Enabled. Applies on the next game launch.' : 'Disabled. Applies on the next game launch.', 'ok'); }
    catch { e.target.checked = !e.target.checked; }
  });
  el.addEventListener('click', async (e) => {
    const button = e.target.closest('button');
    if (!button) return;
    if (button.id === 'mod-install') store.perform('choose_mod').catch(() => {});
    else if (button.dataset.open) store.perform('open', { target: button.dataset.open }).catch(() => {});
    else if (button.dataset.remove) {
      const p = listing?.packages.find((x) => x.id === button.dataset.remove);
      if (!p || !await confirm({ title: `Remove ${p.name}?`, body: 'The package is deleted from your mods folder. Your saves and game files are not changed.', ok: 'Remove', danger: true })) return;
      store.perform('mod_remove', { id: p.id }).then(() => toast(`Removed ${p.name}.`, 'ok')).catch(() => {});
    }
  });
  seen = `${store.state?.mods_revision}:${store.state?.busy}`;
  draw(); load();
}
