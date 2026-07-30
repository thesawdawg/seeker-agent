import { api } from '../core/api-client.js?v=22';
import { $, $$, clear, el } from '../core/dom.js?v=22';
import { showView } from '../core/navigation.js?v=22';
import { state } from '../core/store.js?v=22';
import { toast } from '../components/toast.js?v=22';
import { loadModels } from './auth.js?v=22';
import { openRun } from './run-detail.js?v=22';

/* ── new run ─────────────────────────────────────────────────────────── */

function modelOptions(selected) {
  const options = [el('option', { value: '', text: 'default' })];
  for (const name of state.models) {
    options.push(el('option', { value: name, selected: name === selected, text: name }));
  }
  return options;
}

export function buildModelGrid(container, { completed = [], scope = 'new' } = {}) {
  clear(container);
  if (!state.models.length) {
    container.append(el('p', { class: 'muted small',
      text: 'Could not list models from your provider — defaults will be used.' }));
    return;
  }
  for (const agent of state.agents) {
    const done = completed.includes(agent.name);
    // Scoped so the new-run grid and a break's grid never collide on id
    const id = `model-${scope}-${agent.name}`;
    const tokId = `tok-${scope}-${agent.name}`;
    const tempId = `temp-${scope}-${agent.name}`;
    // Defaults from the agent's profile, if known
    const defTok = agent.max_tokens ?? '';
    const defTemp = agent.temperature ?? '';
    container.append(el('div', { class: `model-row ${done ? 'is-done' : ''}` },
      el('label', { for: id, text: `${agent.name}${done ? ' (already run)' : ''}` }),
      el('select', { id, 'data-agent': agent.name, disabled: done }, modelOptions()),
      el('input', { type: 'number', id: tokId, 'data-agent': agent.name,
        'data-field': 'max_tokens', placeholder: 'tokens',
        value: defTok, min: 256, max: 32768, disabled: done,
        title: 'Max tokens for this agent' }),
      el('input', { type: 'number', id: tempId, 'data-agent': agent.name,
        'data-field': 'temperature', placeholder: 'temp',
        value: defTemp, min: 0, max: 2, step: 0.1, disabled: done,
        title: 'Temperature for this agent' }),
    ));
  }
}

export function collectModelOverrides(container) {
  const overrides = {};
  $$('select[data-agent]', container).forEach(select => {
    if (!select.disabled && select.value) {
      overrides[select.dataset.agent] = { model: select.value };
    }
  });
  // Also collect max_tokens / temperature if they were set (review U4)
  $$('input[data-field]', container).forEach(input => {
    if (input.disabled) return;
    const agent = input.dataset.agent;
    const field = input.dataset.field;
    const val = input.value.trim();
    if (!val) return;
    overrides[agent] = overrides[agent] || {};
    overrides[agent][field] = field === 'temperature' ? parseFloat(val) : parseInt(val, 10);
  });
  return overrides;
}

function currentCredential() {
  const name = $('#new-provider').value;
  return state.providers.find(c => c.provider === name) || state.providers[0] || {};
}

/*
 * Agents choose a model *role*, not a model name, so a provider with no roles
 * assigned has nothing for them to call. A freshly signed-in user has none, so
 * this is asked for before the first run rather than failing mid-pipeline.
 */
function buildRoleGrid() {
  const grid = $('#role-grid');
  clear(grid);
  const models = (currentCredential().models) || {};

  if (!state.models.length) {
    grid.append(el('p', { class: 'muted small',
      text: 'Could not list models from your provider. Check it is reachable, ' +
            'then reload — the pipeline needs at least one model.' }));
    return;
  }

  for (const [role, help] of [
    ['primary', 'Grounder, Historian, Gaper, Vision, Theorist, Rude, Synthesizer, Thinker'],
    ['light',   'Social, Scribe'],
  ]) {
    grid.append(el('div', { class: 'model-row' },
      el('label', { for: `role-${role}`, text: role }),
      el('select', { id: `role-${role}`, 'data-role': role },
        [el('option', { value: '', text: role === 'light' ? 'same as primary' : 'choose a model…' })]
          .concat(state.models.map(name => el('option', {
            value: name, selected: models[role] === name, text: name,
          })))),
      el('span', { class: 'field-hint', text: help }),
    ));
  }
}

function collectRoles() {
  const roles = {};
  $$('#role-grid select[data-role]').forEach(select => {
    if (select.value) roles[select.dataset.role] = select.value;
  });
  if (roles.primary && !roles.light) roles.light = roles.primary;
  return roles;
}

function showNewRun() {
  showView('new');
  const providerSelect = $('#new-provider');
  clear(providerSelect);
  for (const cred of state.providers) {
    providerSelect.append(el('option', { value: cred.provider, text: cred.provider }));
  }
  buildRoleGrid();
  buildModelGrid($('#new-model-grid'));
  buildSourceGrid();
  buildTemplateBar();
  refreshEstimate();
  buildPreviousRunPicker();
  buildBlacklist();
  $('#new-error').hidden = true;
  $('#role-warning').hidden = true;
}

// F5: populate the "Compare to previous run" dropdown with the user's runs.
async function buildPreviousRunPicker() {
  const select = $('#new-previous-run');
  if (!select) return;
  // Keep the "None" option, clear the rest
  const noneOpt = select.querySelector('option');
  clear(select);
  if (noneOpt) select.append(noneOpt);
  try {
    const data = await api('/api/runs');
    const runs = (data.runs || []).filter(r => r.run_id);
    for (const r of runs.slice(0, 50)) {  // cap at 50 for performance
      const label = `${r.run_id.slice(-8)} — ${r.problem.slice(0, 60)}`;
      select.append(el('option', { value: r.run_id, text: label }));
    }
  } catch { /* non-essential */ }
}

// F8: source blacklist — list, add, remove entries.
async function buildBlacklist() {
  const box = $('#new-blacklist');
  if (!box) return;
  clear(box);
  let entries = [];
  try {
    entries = (await api('/api/blacklist')).entries || [];
  } catch { /* non-essential */ }

  // Existing entries
  if (entries.length) {
    const list = el('ul', { class: 'blacklist-list' });
    for (const e of entries) {
      list.append(el('li', {},
        el('span', { class: 'blacklist-type', text: e.match_type }),
        el('span', { class: 'blacklist-value', text: e.match_value }),
        e.reason ? el('span', { class: 'muted small', text: `— ${e.reason}` }) : null,
        el('button', { class: 'btn btn-small', type: 'button',
          onClick: async () => {
            try {
              await api('/api/blacklist', { method: 'DELETE', body: {
                match_type: e.match_type, match_value: e.match_value,
              }});
              buildBlacklist();
            } catch (err) { toast(err.message, 'error'); }
          },
        }, 'Remove'),
      ));
    }
    box.append(list);
  } else {
    box.append(el('p', { class: 'muted small', text: 'No excluded sources yet.' }));
  }

  // Add form
  const typeSelect = el('select', {},
    el('option', { value: 'doi', text: 'DOI' }),
    el('option', { value: 'url', text: 'URL' }),
    el('option', { value: 'title_substring', text: 'Title contains' }),
  );
  const valueInput = el('input', {
    type: 'text', placeholder: 'e.g. 10.1234/abc or "predatory journal"',
    style: 'flex:1; min-width: 200px;',
  });
  const reasonInput = el('input', {
    type: 'text', placeholder: 'reason (optional)',
    style: 'flex:1; min-width: 150px;',
  });
  const addBtn = el('button', { class: 'btn btn-small', type: 'button',
    onClick: async () => {
      if (!valueInput.value.trim()) return;
      try {
        await api('/api/blacklist', { method: 'POST', body: {
          match_type: typeSelect.value,
          match_value: valueInput.value.trim(),
          reason: reasonInput.value.trim(),
        }});
        valueInput.value = '';
        reasonInput.value = '';
        buildBlacklist();
        toast('Added to blacklist', 'ok');
      } catch (err) { toast(err.message, 'error'); }
    },
  }, 'Add');
  box.append(el('div', { class: 'blacklist-add' },
    typeSelect, valueInput, reasonInput, addBtn));
}

// Config template bar (review U10 + F4) — save/restore model + source
// overrides. Built-in templates from config.json (F4) appear alongside
// user-saved templates.
async function buildTemplateBar() {
  const bar = $('#template-bar');
  if (!bar) return;
  clear(bar);

  // Fetch user-saved and built-in templates in parallel.
  const [userRes, builtinRes] = await Promise.all([
    api('/api/templates').catch(() => ({ templates: [] })),
    api('/api/templates/built-in').catch(() => ({ templates: [] })),
  ]);
  const userTpls    = (userRes.templates || []);
  const builtinTpls = (builtinRes.templates || []);

  const allTpls = [
    ...builtinTpls.map(t => ({ ...t, _builtin: true })),
    ...userTpls.map(t => ({ ...t, _builtin: false })),
  ];

  const select = el('select', { id: 'template-select',
                                'aria-label': 'Load a run template' },
    el('option', { value: '', text: '— load template —' }),
    ...builtinTpls.map(t => el('option', { value: `builtin::${t.name}`,
      text: `${t.name} (built-in)` })),
    ...userTpls.length
      ? [el('option', { disabled: true, text: '— your templates —' })]
      : [],
    ...userTpls.map(t => el('option', { value: `user::${t.name}`,
      text: t.name })),
  );
  select.addEventListener('change', async () => {
    const value = select.value;
    if (!value) return;
    const [kind, name] = value.split('::');
    const tpl = (kind === 'builtin' ? builtinTpls : userTpls)
      .find(t => t.name === name);
    if (!tpl) return;
    // Apply template to the form
    const cfg = tpl.config || {};
    const mo = cfg.model_overrides || {};
    const so = cfg.source_overrides || {};
    // Set model selects
    $$('select[data-agent]', $('#new-model-grid')).forEach(sel => {
      const spec = mo[sel.dataset.agent];
      if (spec && spec.model) { sel.value = spec.model; }
    });
    // Set source checkboxes
    $$('input[type=checkbox][data-source]', $('#new-source-grid')).forEach(cb => {
      if (so[cb.dataset.source] !== undefined) cb.checked = so[cb.dataset.source];
    });
    // Set limit
    const lim = $('#new-source-grid input[data-field="limit_per_source"]');
    if (lim && cfg.limit_per_source) lim.value = cfg.limit_per_source;
    else if (lim && so.limit_per_source) lim.value = so.limit_per_source;
    toast(`Loaded template "${name}"`, 'ok');
  });
  const saveBtn = el('button', { class: 'btn btn-small', type: 'button',
    onClick: async () => {
      const name = prompt('Template name:');
      if (!name) return;
      try {
        await api('/api/templates', { method: 'POST', body: {
          name,
          model_overrides: collectModelOverrides($('#new-model-grid')),
          source_overrides: collectSourceOverrides($('#new-source-grid')),
        }});
        toast(`Saved template "${name}"`, 'ok');
        buildTemplateBar();
      } catch (e) { toast(`Could not save: ${e.message}`, 'error'); }
    },
  }, 'Save as template');
  bar.append(el('span', { class: 'muted small', text: 'Templates:' }),
    select, saveBtn);
}

// Pre-flight source health + per-run source enable/disable (review U1 + R8).
// Fetches /api/sources/health once per New Run open and renders a checkbox
// per source with a readiness indicator.
async function buildSourceGrid() {
  const container = $('#new-source-grid');
  if (!container) return;
  clear(container);
  container.append(el('p', { class: 'muted small', text: 'Loading source status…' }));
  let health;
  try {
    health = await api('/api/sources/health');
  } catch (err) {
    clear(container);
    container.append(el('p', { class: 'muted small',
      text: `Could not load source status: ${err.message}` }));
    return;
  }
  clear(container);
  const sources = health.sources || [];
  if (!sources.length) {
    container.append(el('p', { class: 'muted small',
      text: 'No sources configured in config.json.' }));
    return;
  }
  for (const s of sources) {
    const checked = s.enabled;
    const indicator = s.has_key ? '✓' : '⚠';
    const indicatorClass = s.has_key ? 'src-ok' : 'src-warn';
    const note = s.note || '';
    container.append(el('label', { class: 'source-row' },
      el('input', { type: 'checkbox', 'data-source': s.source_id, checked }),
      el('span', { class: 'source-name', text: s.source_id }),
      el('span', { class: `source-indicator ${indicatorClass}`, text: indicator,
                   title: note }),
      el('span', { class: 'muted small', text: note }),
    ));
  }
  // Any change to the selection changes the estimate above.
  container.addEventListener('change', scheduleEstimate);
  container.addEventListener('input', scheduleEstimate);

  // Per-source result limit control (review U5)
  container.append(el('div', { class: 'source-limit-row' },
    el('label', { for: 'src-limit', text: 'Results per source',
                  title: 'How many results to fetch from each source. Lower = faster shallow scan, higher = deeper coverage.' }),
    el('input', { type: 'number', id: 'src-limit',
                  'data-field': 'limit_per_source',
                  value: 8, min: 1, max: 50 }),
    el('span', { class: 'muted small', text: 'shallow ↔ deep' }),
  ));
}

export function collectSourceOverrides(container) {
  const overrides = {};
  for (const cb of container.querySelectorAll('input[type=checkbox][data-source]')) {
    overrides[cb.dataset.source] = cb.checked;
  }
  // Per-run source result limit (review U5) — shallow vs deep scan control.
  const lim = container.querySelector('input[data-field="limit_per_source"]');
  if (lim && lim.value.trim()) {
    overrides.limit_per_source = parseInt(lim.value, 10);
  }
  return overrides;
}

/* ── Pre-run estimate (review X2) ────────────────────────────────────────
 * A run is a long, expensive, human-blocking commitment and this screen used
 * to give no sense of its scale before the Start button. Refreshed whenever
 * the source selection changes, so turning sources off visibly shrinks it.  */

let estimateTimer = null;

function scheduleEstimate() {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(refreshEstimate, 250);
}

async function refreshEstimate() {
  const box = $('#new-estimate');
  if (!box) return;
  const grid = $('#new-source-grid');
  const overrides = grid ? collectSourceOverrides(grid) : {};
  try {
    const params = new URLSearchParams({
      source_overrides: JSON.stringify(overrides),
    });
    const est = await api(`/api/runs/estimate?${params}`);
    clear(box);
    box.hidden = false;

    const mins = Math.max(1, Math.round(est.estimated_seconds / 60));
    box.append(
      el('h3', { class: 'estimate-title', text: 'Before you start' }),
      el('ul', { class: 'estimate-list' },
        el('li', {}, `${est.themes} theme(s) × ${est.sources.length} source(s) `
                   + `× ${est.results_per_source} results — `,
          el('strong', { text: `${est.source_lookups.toLocaleString()} source lookups` })),
        el('li', {}, 'Roughly ',
          el('strong', { text: `${est.estimated_calls.toLocaleString()} model calls` }),
          ` · ~${est.estimated_tokens.toLocaleString()} tokens`),
        el('li', {}, 'Roughly ', el('strong', { text: `${mins} minutes` }),
          ' of compute, plus ',
          el('strong', { text: `${est.breaks} breaks` }),
          ' that wait for you'),
      ),
      el('p', { class: 'muted small estimate-basis',
                text: est.is_measured
                  ? `Based on ${est.basis}. A rough guide, not a quote.`
                  : `Based on ${est.basis}. Expect this to be well off until `
                    + `you have run the pipeline once.` }),
    );
  } catch {
    box.hidden = true;      // non-essential; never block starting a run
  }
}

export function wireNewRun() {
  $('#btn-new-run').addEventListener('click', showNewRun);
  $('#new-provider').addEventListener('change', () => {
    loadModels().then(() => { buildRoleGrid(); buildModelGrid($('#new-model-grid')); });
  });

  $('#form-new-run').addEventListener('submit', async ev => {
    ev.preventDefault();
    const button = $('#btn-start-run');
    const error = $('#new-error');
    error.hidden = true;

    // Without a primary model the run would start and then fail at the first
    // agent, so it is refused here instead.
    const roles = collectRoles();
    if (!roles.primary) {
      $('#role-warning').hidden = false;
      $('#role-primary')?.focus();
      return;
    }
    $('#role-warning').hidden = true;

    button.disabled = true;
    button.textContent = 'Starting…';

    try {
      const provider = $('#new-provider').value;
      const saved = await api(
        `/api/credentials/${encodeURIComponent(provider)}/models`,
        { method: 'PATCH', body: { models: roles } });
      const index = state.providers.findIndex(c => c.provider === provider);
      if (index >= 0) state.providers[index] = saved.credential;

      const body = await api('/api/runs', {
        method: 'POST',
        body: {
          problem:  $('#new-problem').value.trim(),
          provider: $('#new-provider').value,
          model_overrides: collectModelOverrides($('#new-model-grid')),
          source_overrides: collectSourceOverrides($('#new-source-grid')),
          previous_run_id: ($('#new-previous-run') || {}).value || '',
        },
      });
      $('#new-problem').value = '';
      toast('Run queued — a worker will pick it up.', 'ok');
      await openRun(body.run_id);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = 'Start run';
    }
  });
}
