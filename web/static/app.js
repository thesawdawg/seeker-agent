/*
 * SEEKER — web interface
 *
 * Plain fetch + DOM. No build step, no framework, no external assets.
 *
 * Progress is polled: GET /api/runs/{id}/status every couple of seconds while
 * work is moving, backing off once a run parks at a break, because nothing
 * changes until a human acts.
 *
 * The break screens are the point of this UI. Each widget generates a line of
 * the same directive language the CLI uses (REMOVE GAP GAP-3, SCRIBE OUTPUT:
 * ... | audience: ...), and the exact text being submitted is always shown, so
 * the interface stays honest about what it is doing on your behalf.
 */

'use strict';

const POLL_ACTIVE_MS = 2000;   // work is moving
const POLL_IDLE_MS   = 15000;  // parked at a break — nothing changes unaided

const state = {
  user: null,
  runId: null,
  status: null,
  steps: [],          // pipeline shape, from /api/steps
  agents: [],         // agent names + defaults
  models: [],         // models the user's provider serves
  providers: [],      // configured credentials
  tab: 'overview',
  breakDraft: null,   // in-progress break edits
  pollTimer: null,
  activityLog: [],    // accumulated service notes for this run
  activitySeen: null, // dedupe key set, reset per run
  elapsedTimer: null,
};

/* ── tiny helpers ────────────────────────────────────────────────────── */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function toast(message, kind = '') {
  const node = el('div', { class: `toast ${kind ? 'is-' + kind : ''}`, text: message });
  $('#toasts').append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 7000 : 3500);
}

function shortTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

/* ── API ─────────────────────────────────────────────────────────────── */

async function api(path, options = {}) {
  const opts = { credentials: 'same-origin', headers: {}, ...options };
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }

  const resp = await fetch(path, opts);
  if (resp.status === 401 && !path.includes('/auth/login')) {
    stopPolling();
    state.user = null;
    showView('login');
    throw new Error('Your session expired — sign in again.');
  }

  let payload = null;
  try { payload = await resp.json(); } catch { /* empty body */ }

  if (!resp.ok) {
    const detail = payload && payload.detail;
    throw new Error(typeof detail === 'string' ? detail
      : `Request failed (HTTP ${resp.status})`);
  }
  return payload;
}

/* ── view switching ──────────────────────────────────────────────────── */

function showView(name) {
  $$('.view').forEach(v => { v.hidden = v.id !== `view-${name}`; });
  $('#topbar').hidden = (name === 'login');
  if (name !== 'run') stopPolling();
}

/* ── sign in ─────────────────────────────────────────────────────────── */

const BASE_URL_DEFAULTS = {
  'open-webui': 'http://localhost:3000/api',
  'openai':     'https://api.openai.com/v1',
  'ollama':     'http://localhost:11434/v1',
  'lm-studio':  'http://localhost:1234/v1',
  'vllm':       'http://localhost:8000/v1',
  'openrouter': 'https://openrouter.ai/api/v1',
  'anthropic':  'https://api.anthropic.com',
};

function wireLogin() {
  $('#login-provider').addEventListener('change', ev => {
    const preset = BASE_URL_DEFAULTS[ev.target.value];
    if (preset) $('#login-base-url').value = preset;
    $('#login-base-hint').innerHTML = ev.target.value === 'open-webui'
      ? 'Open-WebUI: include the <code>/api</code> prefix.'
      : 'The endpoint root, without <code>/chat/completions</code>.';
  });

  $('#form-login').addEventListener('submit', async ev => {
    ev.preventDefault();
    const button = $('#btn-login');
    const error = $('#login-error');
    error.hidden = true;
    button.disabled = true;
    button.textContent = 'Checking your key…';

    try {
      await api('/api/auth/login', {
        method: 'POST',
        body: {
          provider:     $('#login-provider').value,
          base_url:     $('#login-base-url').value.trim(),
          api_key:      $('#login-api-key').value.trim(),
          display_name: $('#login-name').value.trim(),
        },
      });
      $('#login-api-key').value = '';
      await afterSignIn();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = 'Sign in';
    }
  });

  $('#btn-logout').addEventListener('click', async () => {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* ignore */ }
    state.user = null;
    showView('login');
  });

  // Theme toggle (review U11) — remembers the choice in localStorage.
  const savedTheme = localStorage.getItem('seeker-theme');
  if (savedTheme) document.documentElement.setAttribute('data-theme', savedTheme);
  $('#btn-theme').addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    const isDark = current
      ? current === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('seeker-theme', next);
  });
}

async function afterSignIn() {
  const me = await api('/api/auth/me');
  state.user = me;
  state.providers = me.credentials || [];
  $('#user-chip').textContent = me.display_name || me.user_id;

  const [shape, agents] = await Promise.all([
    api('/api/steps'), api('/api/agents'),
  ]);
  state.steps = shape.steps;
  state.agents = agents.agents;

  await loadModels();
  await showRuns();
}

async function loadModels() {
  const provider = (state.providers[0] || {}).provider;
  if (!provider) { state.models = []; return; }
  try {
    const body = await api(`/api/models?provider=${encodeURIComponent(provider)}`);
    state.models = body.models || [];
  } catch {
    state.models = [];   // provider unreachable; pickers fall back to free text
  }
}

/* ── run list ────────────────────────────────────────────────────────── */

function runPill(run) {
  if (run.awaiting_break !== null && run.awaiting_break !== undefined)
    return el('span', { class: 'pill pill-break', text: `Break ${run.awaiting_break}` });
  if ((run.status || '').startsWith('failed'))
    return el('span', { class: 'pill pill-failed', text: 'Failed' });
  if (run.status === 'completed')
    return el('span', { class: 'pill pill-done', text: 'Complete' });
  if (run.status === 'cancelled')
    return el('span', { class: 'pill', text: 'Stopped' });
  if (run.status === 'cancelling')
    return el('span', { class: 'pill', text: 'Stopping…' });
  return el('span', { class: 'pill pill-active', text: 'Running' });
}

async function showRuns() {
  showView('runs');
  const list = $('#runs-list');
  clear(list);

  const { runs } = await api('/api/runs');
  $('#runs-empty').hidden = runs.length > 0;

  for (const run of runs) {
    list.append(el('div', { class: 'run-card', onClick: () => openRun(run.run_id) },
      el('div', {},
        el('div', { class: 'run-card-problem', text: run.problem }),
        el('div', { class: 'run-card-meta',
                    text: `${run.run_id} · ${shortTime(run.created_at)}` }),
      ),
      el('div', { class: 'run-card-right' },
        runPill(run),
        el('div', { class: 'run-card-meta',
                    text: `${run.progress.done}/${run.progress.total} steps` }),
      ),
    ));
  }
}

/* ── new run ─────────────────────────────────────────────────────────── */

function modelOptions(selected) {
  const options = [el('option', { value: '', text: 'default' })];
  for (const name of state.models) {
    options.push(el('option', { value: name, selected: name === selected, text: name }));
  }
  return options;
}

function buildModelGrid(container, { completed = [], scope = 'new' } = {}) {
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

function collectModelOverrides(container) {
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
  buildSourceKeys();
  buildTemplateBar();
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

  const select = el('select', { id: 'template-select' },
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

function collectSourceOverrides(container) {
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

// Per-user academic source API keys (review U2). Lets a researcher add a
// Scopus/CORE/... key inline on the New Run screen.
function buildSourceKeys() {
  const container = $('#new-source-keys');
  if (!container) return;
  clear(container);
  const keyable = ['scopus', 'semantic_scholar', 'core', 'google_books',
                   'philpapers', 'openalex', 'pubmed'];
  const row = el('div', { class: 'source-key-row' },
    el('select', { id: 'src-key-source' },
      ...keyable.map(s => el('option', { value: s, text: s }))),
    el('input', { type: 'password', id: 'src-key-value',
                  placeholder: 'API key', autocomplete: 'off' }),
    el('button', { class: 'btn btn-small', type: 'button', id: 'btn-save-src-key',
                   onClick: saveSourceKey }, 'Save'),
  );
  container.append(row);
  // List existing source keys
  api('/api/source-credentials').then(res => {
    const list = (res.credentials || []);
    if (!list.length) return;
    container.append(el('div', { class: 'source-key-list' },
      ...list.map(c => el('div', { class: 'source-key-item' },
        el('span', { text: c.source_id }),
        el('span', { class: 'muted small', text: c.key_hint || '••••' }),
        el('button', { class: 'btn btn-ghost btn-small', type: 'button',
          onClick: () => deleteSourceKey(c.source_id) }, 'Remove'),
      )),
    ));
  }).catch(() => {});
}

async function saveSourceKey() {
  const sourceId = $('#src-key-source').value;
  const value = $('#src-key-value').value;
  if (!value) return;
  try {
    await api('/api/source-credentials', {
      method: 'PUT', body: { source_id: sourceId, api_key: value } });
    $('#src-key-value').value = '';
    toast(`Key for ${sourceId} saved.`, 'ok');
    buildSourceKeys();
    buildSourceGrid();
  } catch (err) { toast(err.message, 'error'); }
}

async function deleteSourceKey(sourceId) {
  try {
    await api(`/api/source-credentials/${encodeURIComponent(sourceId)}`,
              { method: 'DELETE' });
    toast(`Key for ${sourceId} removed.`, 'ok');
    buildSourceKeys();
    buildSourceGrid();
  } catch (err) { toast(err.message, 'error'); }
}

function wireNewRun() {
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

/* ── run detail ──────────────────────────────────────────────────────── */

async function openRun(runId) {
  state.runId = runId;
  state.tab = 'overview';
  state.breakDraft = null;
  state.activityLog = [];
  state.activitySeen = new Set();
  state._lastDetailSig = null;  // force a fresh detail fetch for the new run
  showView('run');

  const detail = await api(`/api/runs/${runId}`);
  $('#run-problem').textContent = detail.problem;
  $('#run-meta').textContent = `${runId} · started ${shortTime(detail.created_at)}`;

  await refreshStatus();
  startPolling();
}

function startPolling() {
  stopPolling();
  const tick = async () => {
    if (!state.runId) return;
    try {
      const moving = await refreshStatus();
      state.pollTimer = setTimeout(tick, moving ? POLL_ACTIVE_MS : POLL_IDLE_MS);
    } catch {
      // Transient failure: keep the loop alive but ease off
      state.pollTimer = setTimeout(tick, POLL_IDLE_MS);
    }
  };
  state.pollTimer = setTimeout(tick, POLL_ACTIVE_MS);
}

function stopPolling() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = null;
  stopElapsedTicker();
}

async function refreshStatus() {
  const previous = state.status;
  const status = await api(`/api/runs/${state.runId}/status`);
  state.status = status;

  if (!state.activitySeen) state.activitySeen = new Set();
  recordActivity(status);

  renderRail(status);
  renderTabs(status);
  if (state.tab === 'overview') renderOverview(status);

  // Announce arrival at a break, and open it once
  const arrived = status.awaiting_break !== null &&
                  (!previous || previous.awaiting_break !== status.awaiting_break);
  if (arrived) {
    toast(`Break ${status.awaiting_break} is ready for your review.`, 'ok');
    await openBreak(status.awaiting_break);
  }
  if (previous && !previous.complete && status.complete) {
    toast('Pipeline complete.', 'ok');
    if (state.tab === 'artifacts') renderArtifacts();
  }
  if (status.failed_steps.length &&
      (!previous || !previous.failed_steps.length)) {
    toast(`Step failed: ${status.failed_steps.join(', ')}`, 'error');
  }

  const live = status.running || status.queued || status.status === 'cancelling';
  $('#live-dot').classList.toggle('is-live', live);
  return live;
}

const STEP_ICON = {
  pending: '·', running: '', awaiting_input: '⏸',
  done: '✓', failed: '✗', skipped: '⊘',
};

function renderRail(status) {
  const rail = $('#step-rail');
  clear(rail);

  $('#progress-count').textContent = `${status.progress.done}/${status.progress.total}`;
  $('#progress-fill').style.width =
    `${(status.progress.done / status.progress.total) * 100}%`;

  for (const step of status.steps) {
    const isBreak = step.name.startsWith('break');
    const icon = step.status === 'running'
      ? el('span', { class: 'spinner' })
      : el('span', { text: STEP_ICON[step.status] || '·' });

    // Where the run stands, and nothing more. What a step is doing right now
    // belongs to the live card and activity log, so the rail stays a stable
    // map of the pipeline rather than a second, competing feed.
    let warnings = [];
    try { warnings = JSON.parse(step.warnings || '[]'); } catch { warnings = []; }
    const row = el('li', {
      class: `rail-step status-${step.status} ${isBreak ? 'is-break' : ''} ` +
             `${step.name === status.current_step ? 'is-current' : ''} ` +
             `${warnings.length ? 'has-warnings' : ''}`,
      title: step.error || step.label,
    },
      el('span', { class: 'rail-step-icon' }, icon),
      el('span', { class: 'rail-step-label', text: step.label }),
      warnings.length ? el('span', { class: 'rail-warn-badge',
        title: `${warnings.length} warning(s): ${warnings.map(w => w.message).join('; ')}`,
        text: '⚠' }) : null,
    );

    if (isBreak && ['done', 'awaiting_input'].includes(step.status)) {
      const num = Number(step.name.replace('break', ''));
      row.append(el('button', {
        class: 'rail-rerun', type: 'button', title: 'Review this break',
        onClick: ev => { ev.stopPropagation(); openBreak(num); },
      }, '↗'));
    } else if (['done', 'failed', 'skipped'].includes(step.status)) {
      row.append(el('button', {
        class: 'rail-rerun', type: 'button', title: 'Re-run this step',
        onClick: ev => { ev.stopPropagation(); confirmRerun(step.name); },
      }, '↻'));
    }
    rail.append(row);
  }
}

function renderTabs(status) {
  $('#tab-break').hidden = status.awaiting_break === null;
  if (status.awaiting_break !== null) {
    $('#tab-break').textContent = `Break ${status.awaiting_break}`;
  }
}

/*
 * The overview is rebuilt in place rather than from scratch on each poll, so
 * the activity log keeps its history and the page does not flicker every two
 * seconds.
 */
function ensureOverviewSkeleton() {
  const panel = $('#panel-overview');
  if ($('#live-card', panel)) return panel;
  clear(panel);
  panel.append(
    el('div', { id: 'live-card' }),
    el('div', { id: 'stat-grid', class: 'stat-grid' }),
    el('div', { id: 'routing-note' }),
    el('div', { class: 'review-group' },
      el('h3', {},
        'Activity log',
        el('span', { class: 'muted small', id: 'activity-count' })),
      el('p', { class: 'muted small',
                text: 'Every service the pipeline contacts, newest last.' }),
      el('div', { class: 'activity-log', id: 'activity-log' },
        el('p', { class: 'muted small', text: 'Waiting for the first step…' })),
    ),
  );
  return panel;
}

function fmtDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}

function stepElapsed(step) {
  if (!step || !step.started_at) return null;
  const start = new Date(step.started_at).getTime();
  if (Number.isNaN(start)) return null;
  const end = step.finished_at ? new Date(step.finished_at).getTime() : Date.now();
  return end - start;
}

/* Per-source progress for Grounder/Social steps (review U8). Fetches the
 * /api/runs/{id}/sources endpoint and renders a compact list of which
 * sources have been searched so far and their result counts. */
async function refreshSourceProgress() {
  const container = $('#source-progress');
  if (!container || !state.runId) return;
  try {
    const data = await api(`/api/runs/${state.runId}/sources`);
    const health = data.health || [];
    if (!health.length) {
      container.textContent = 'Searching sources…';
      return;
    }
    clear(container);
    const total = health.length;
    const ok = health.filter(h => h.status === 'ok').length;
    const degraded = health.filter(h => h.status === 'degraded').length;
    const failed = health.filter(h => h.status === 'failed').length;
    container.append(el('div', { class: 'source-progress-summary' },
      el('span', { text: `${total} source call(s) so far` }),
      ok ? el('span', { class: 'src-ok', text: `${ok} ok` }) : null,
      degraded ? el('span', { class: 'src-warn', text: `${degraded} partial` }) : null,
      failed ? el('span', { class: 'src-fail', text: `${failed} failed` }) : null,
    ));
    const list = el('div', { class: 'source-progress-list' });
    for (const h of health.slice(-12)) {
      list.append(el('div', { class: `source-progress-row src-${h.status}` },
        el('span', { class: 'source-progress-name', text: h.source_id }),
        el('span', { class: 'source-progress-count',
                     text: `${h.results_returned || 0} results` }),
        // Skip a problematic source for the rest of the run (review U9).
        // Won't interrupt an in-flight call, but prevents future calls.
        el('button', { class: 'btn btn-ghost btn-small', type: 'button',
          title: 'Disable this source for the rest of the run',
          onClick: async () => {
            try {
              await api(`/api/runs/${state.runId}/sources/override`, {
                method: 'PUT',
                body: JSON.stringify({ [h.source_id]: false }),
              });
              toast(`Disabled ${h.source_id} for this run`, 'ok');
              refreshSourceProgress();
            } catch (e) { toast(`Could not disable: ${e.message}`, 'error'); }
          },
        }, 'skip'),
      ));
    }
    container.append(list);
  } catch { /* non-essential */ }
}

/* The prominent "what is happening right now" card. */
function renderLiveCard(status) {
  const card = $('#live-card');
  if (!card) return;
  clear(card);

  const running = status.steps.find(s => s.status === 'running');
  const shape = running ? state.steps.find(s => s.name === running.name) : null;
  const services = shape ? (shape.services || []).map(s => s.label) : [];
  const position = running
    ? status.steps.findIndex(s => s.name === running.name) + 1 : 0;

  if (status.failed_steps.length) {
    const failed = status.steps.find(s => s.status === 'failed');
    const errText = (failed && failed.error) || '';
    const isLLMError = /all llm providers|no usable llm provider/i.test(errText);
    card.append(el('div', { class: 'live-card is-failed' },
      el('div', { class: 'live-head' },
        el('h2', { text: `Stopped at ${failed ? failed.label : status.failed_steps[0]}` })),
      el('p', { class: 'live-activity', text: errText }),
      el('div', { class: 'live-actions' },
        el('button', { class: 'btn btn-small', type: 'button',
                       onClick: () => retryRun() }, 'Retry this step'),
        isLLMError
          ? el('button', { class: 'btn btn-ghost btn-small', type: 'button',
              onClick: () => {
                toast('Change model routing in Settings, then click Retry.', 'ok');
                document.querySelector('[data-nav="runs"]')?.click();
              } }, 'Change models')
          : null,
      ),
    ));
    return;
  }

  if (status.status === 'cancelling') {
    card.append(el('div', { class: 'live-card is-stopping' },
      el('div', { class: 'live-head' },
        el('span', { class: 'spinner spinner-lg' }),
        el('h2', { text: 'Stopping…' })),
      el('p', { class: 'live-activity',
                text: 'Waiting for the current call to return. The step being '
                    + 'run will be discarded so it can restart cleanly.' }),
      el('p', { class: 'live-meta', text:
        `If it does not stop within ${status.stop_grace_seconds || 60}s it is `
        + 'abandoned automatically.' }),
      el('div', { class: 'live-actions' },
        el('button', { class: 'btn btn-small', type: 'button', id: 'btn-force-stop',
                       onClick: forceStopRun }, "Stop now, don't wait")),
    ));
    return;
  }

  if (status.status === 'cancelled') {
    card.append(renderStoppedCard(status));
    return;
  }

  if (status.awaiting_break !== null) {
    card.append(el('div', { class: 'live-card is-break' },
      el('div', { class: 'live-head' },
        el('h2', { text: `Break ${status.awaiting_break} — your turn` })),
      el('p', { class: 'live-activity',
                text: 'The pipeline is paused. Nothing runs until you respond.' }),
      el('button', { class: 'btn btn-primary btn-small', type: 'button',
                     onClick: () => openBreak(status.awaiting_break) },
        'Review and respond'),
    ));
    return;
  }

  if (status.complete) {
    card.append(el('div', { class: 'live-card is-done' },
      el('div', { class: 'live-head' }, el('h2', { text: 'Pipeline complete' })),
      el('p', { class: 'live-activity',
                text: 'All 14 steps finished. Your outputs are under Artifacts.' }),
    ));
    return;
  }

  if (running) {
    const isSourceStep = ['grounder', 'social'].includes(running.name);
    // ETA based on average duration of completed steps in this run (review U7).
    const completed = status.steps.filter(s =>
      s.status === 'done' && s.started_at && s.finished_at);
    let etaText = '';
    if (completed.length >= 2) {
      const avgMs = completed.reduce((sum, s) =>
        sum + (new Date(s.finished_at) - new Date(s.started_at)), 0) / completed.length;
      const remaining = status.progress.total - status.progress.done - 1;
      if (remaining > 0) {
        const etaMs = avgMs * remaining;
        etaText = ` · ETA ~${fmtDuration(etaMs)} (${remaining} steps left)`;
      }
    }
    card.append(el('div', { class: 'live-card is-running' },
      el('div', { class: 'live-head' },
        el('span', { class: 'spinner spinner-lg' }),
        el('h2', { text: running.label }),
        el('span', { class: 'live-elapsed', id: 'live-elapsed',
                     'data-started': running.started_at || '',
                     text: fmtDuration(stepElapsed(running)) }),
      ),
      el('p', { class: 'live-activity', id: 'live-activity',
                text: running.activity || 'starting…' }),
      el('p', { class: 'live-meta', text:
        `Step ${position} of ${status.progress.total}` +
        (services.length ? ` · uses ${services.join(', ')}` : '') + etaText }),
      // Per-source progress during Grounder/Social (review U8). Polled
      // alongside the status poll — see refreshSourceProgress().
      isSourceStep ? el('div', { id: 'source-progress',
                                 class: 'source-progress' }) : null,
      el('div', { class: 'live-actions' },
        el('button', { class: 'btn btn-small', type: 'button', id: 'btn-stop',
                       onClick: stopRun },
          'Stop and change model')),
    ));
    if (isSourceStep) refreshSourceProgress();
    return;
  }

  card.append(el('div', { class: 'live-card' },
    el('div', { class: 'live-head' },
      el('span', { class: 'spinner spinner-lg' }),
      el('h2', { text: status.queued ? 'Queued' : 'Starting' })),
    el('p', { class: 'live-activity',
              text: status.queued
                ? 'Waiting for a worker to pick this run up…'
                : 'Preparing the next step…' }),
  ));
}

/*
 * A stopped run: say what was discarded, let the models be changed, resume.
 *
 * This is the whole point of stopping — the usual reason is that the run is
 * using the wrong model, so the fix must be reachable from here rather than
 * requiring a new run.
 */
function renderStoppedCard(status) {
  const next = status.steps.find(s => s.status === 'pending');
  const cred = state.providers.find(c => c.provider === currentProviderName()) || {};
  const roles = cred.models || {};

  const card = el('div', { class: 'live-card is-stopped' },
    el('div', { class: 'live-head' }, el('h2', { text: 'Stopped' })),
    el('p', { class: 'live-activity', text: next
      ? `${next.label} was discarded and will run again from the beginning.`
      : 'The run is stopped.' }),
    el('p', { class: 'live-meta',
      text: `${status.progress.done} of ${status.progress.total} steps completed — those are kept.` }),
  );

  if (state.models.length) {
    const grid = el('div', { class: 'model-grid', id: 'resume-roles' });
    for (const [role, help] of [
      ['primary', 'heavy reasoning agents'],
      ['light',   'Social and Scribe'],
    ]) {
      grid.append(el('div', { class: 'model-row' },
        el('label', { for: `resume-${role}`, text: role }),
        el('select', { id: `resume-${role}`, 'data-role': role },
          state.models.map(name => el('option', {
            value: name, selected: roles[role] === name, text: name }))),
        el('span', { class: 'field-hint', text: help }),
      ));
    }
    card.append(
      el('h3', { class: 'resume-heading' }, 'Models for the remaining steps'),
      grid,
    );
  } else {
    card.append(el('p', { class: 'muted small',
      text: 'Could not list models from your provider — resuming will reuse the current selection.' }));
  }

  card.append(el('div', { class: 'live-actions' },
    el('button', { class: 'btn btn-primary btn-small', type: 'button',
                   id: 'btn-resume', onClick: resumeRun },
      'Resume with these models'),
  ));
  return card;
}

function currentProviderName() {
  return (state.providers[0] || {}).provider || 'open-webui';
}

async function stopRun() {
  const button = $('#btn-stop');
  if (button) { button.disabled = true; button.textContent = 'Stopping…'; }
  try {
    const result = await api(`/api/runs/${state.runId}/stop`, { method: 'POST' });
    toast(result.immediate
      ? 'Run stopped.'
      : 'Stopping — waiting for the current model call to return.', 'ok');
    await refreshStatus();
    startPolling();
  } catch (err) {
    toast(err.message, 'error');
    if (button) { button.disabled = false; button.textContent = 'Stop and change model'; }
  }
}

async function forceStopRun() {
  const button = $('#btn-force-stop');
  if (button) { button.disabled = true; button.textContent = 'Stopping…'; }
  try {
    await api(`/api/runs/${state.runId}/stop/force`, { method: 'POST' });
    toast('Stopped. The interrupted step was discarded.', 'ok');
    await refreshStatus();
    startPolling();
  } catch (err) {
    toast(err.message, 'error');
    if (button) { button.disabled = false; button.textContent = "Stop now, don't wait"; }
  }
}

async function resumeRun() {
  const button = $('#btn-resume');
  if (button) { button.disabled = true; button.textContent = 'Resuming…'; }

  const models = {};
  $$('#resume-roles select[data-role]').forEach(sel => {
    if (sel.value) models[sel.dataset.role] = sel.value;
  });

  try {
    await api(`/api/runs/${state.runId}/resume`, {
      method: 'POST',
      body: { provider: currentProviderName(), models },
    });
    // Keep the local copy in step with what the server now holds
    const cred = state.providers.find(c => c.provider === currentProviderName());
    if (cred && Object.keys(models).length) cred.models = models;

    toast('Resumed.', 'ok');
    await refreshStatus();
    startPolling();
  } catch (err) {
    toast(err.message, 'error');
    if (button) { button.disabled = false; button.textContent = 'Resume with these models'; }
  }
}

/*
 * Accumulate distinct activity notes as polling observes them, so the page
 * shows a history of what was contacted rather than only the latest line.
 */
function recordActivity(status) {
  let added = 0;
  for (const step of status.steps) {
    if (!step.activity) continue;
    const key = `${step.name}|${step.activity}`;
    if (state.activitySeen.has(key)) continue;
    state.activitySeen.add(key);
    state.activityLog.push({
      at: step.activity_at || new Date().toISOString(),
      step: step.label,
      text: step.activity,
    });
    added++;
  }
  if (added) {
    state.activityLog.sort((a, b) => (a.at || '').localeCompare(b.at || ''));
    // Keep memory bounded on very long runs
    if (state.activityLog.length > 400) {
      state.activityLog = state.activityLog.slice(-400);
    }
  }
  return added;
}

function renderActivityLog() {
  const log = $('#activity-log');
  if (!log) return;
  clear(log);

  const count = $('#activity-count');
  if (count) {
    count.textContent = state.activityLog.length
      ? `${state.activityLog.length} events` : '';
  }

  if (!state.activityLog.length) {
    log.append(el('p', { class: 'muted small',
      text: 'Nothing reported yet — the first step will appear here.' }));
    return;
  }

  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  for (const entry of state.activityLog) {
    const [service, ...rest] = entry.text.split(' — ');
    log.append(el('div', { class: 'activity-row' },
      el('span', { class: 'activity-time',
                   text: (entry.at || '').slice(11, 19) }),
      el('span', { class: 'activity-step', text: entry.step }),
      el('span', { class: 'activity-service', text: service }),
      el('span', { class: 'activity-detail', text: rest.join(' — ') }),
    ));
  }
  // Follow the tail unless the reader has scrolled up to look at history
  if (atBottom) log.scrollTop = log.scrollHeight;
}

/* A one-second timer so elapsed time moves between polls. */
function startElapsedTicker() {
  stopElapsedTicker();
  state.elapsedTimer = setInterval(() => {
    const node = $('#live-elapsed');
    if (!node) return;
    const started = node.dataset.started;
    if (!started) return;
    const ms = Date.now() - new Date(started).getTime();
    node.textContent = fmtDuration(ms);
  }, 1000);
}

function stopElapsedTicker() {
  if (state.elapsedTimer) clearInterval(state.elapsedTimer);
  state.elapsedTimer = null;
}

function renderOverview(status) {
  ensureOverviewSkeleton();
  renderLiveCard(status);
  renderActivityLog();

  if (status.running || status.queued) startElapsedTicker();
  else stopElapsedTicker();

  // Only re-fetch the heavy /api/runs/{id} endpoint (7 count queries) when
  // the step state has actually changed — not on every 2s poll (review O5).
  // The signature is built from each step's status+ordinal, so a step
  // transitioning pending→running→done triggers a re-fetch, but idle polls
  // during a long step do not.
  const sig = (status.steps || [])
    .map(s => `${s.name}:${s.status}:${s.attempt || 0}`).join('|');
  if (state._lastDetailSig === sig) return;
  state._lastDetailSig = sig;

  api(`/api/runs/${state.runId}`).then(detail => {
    if (state.runId !== detail.run_id) return;
    const grid = $('#stat-grid');
    if (grid) {
      clear(grid);
      const counts = detail.counts || {};
      for (const [key, label] of [
        ['sources', 'Sources'], ['gaps', 'Gaps'], ['implications', 'Implications'],
        ['proposals', 'Proposals'], ['evaluations', 'Evaluations'],
        ['directions', 'Directions'], ['artifacts', 'Artifacts'],
      ]) {
        grid.append(el('div', { class: 'stat' },
          el('div', { class: 'stat-value', text: counts[key] ?? 0 }),
          el('div', { class: 'stat-label', text: label }),
        ));
      }
    }

    const note = $('#routing-note');
    if (note) {
      clear(note);
      const overrides = detail.model_overrides || {};
      const srcOverrides = detail.source_overrides || {};
      if (Object.keys(overrides).length) {
        note.append(el('div', { class: 'review-group' },
          el('h3', {}, 'Model routing for this run'),
          el('div', { class: 'directive-preview',
            text: Object.entries(overrides)
              .map(([agent, spec]) => `${agent}: ${spec.model || spec.provider || ''}`)
              .join('\n') }),
        ));
      }
      const offSources = Object.entries(srcOverrides)
        .filter(([, v]) => v === false).map(([k]) => k);
      const onSources = Object.entries(srcOverrides)
        .filter(([, v]) => v === true).map(([k]) => k);
      if (offSources.length || onSources.length) {
        note.append(el('div', { class: 'review-group' },
          el('h3', {}, 'Sources for this run'),
          el('div', { class: 'directive-preview',
            text: [
              onSources.length ? `Forced on: ${onSources.join(', ')}` : '',
              offSources.length ? `Forced off: ${offSources.join(', ')}` : '',
            ].filter(Boolean).join('\n') }),
        ));
      }

      // LLM token usage (F10) — fetched in parallel, non-essential.
      api(`/api/runs/${state.runId}/usage`).then(usage => {
        if (state.runId !== detail.run_id) return;
        if (!usage.total_calls) return;
        const usageBox = el('div', { class: 'review-group' },
          el('h3', {}, 'Token usage',
            el('span', { class: 'muted small',
              text: ` — ${usage.total_calls} calls, ${usage.total_tokens.toLocaleString()} tokens` })),
        );
        // By agent table
        const byAgent = usage.by_agent || {};
        const agentRows = Object.entries(byAgent)
          .sort((a, b) => b[1].total - a[1].total);
        if (agentRows.length) {
          const tbl = el('table', { class: 'usage-table' },
            el('thead', {}, el('tr', {},
              el('th', { text: 'Agent' }),
              el('th', { text: 'Calls' }),
              el('th', { text: 'Prompt' }),
              el('th', { text: 'Completion' }),
              el('th', { text: 'Total' }),
            )),
          );
          const tbody = el('tbody');
          for (const [agent, u] of agentRows) {
            tbody.append(el('tr', {},
              el('td', { text: agent }),
              el('td', { text: String(u.calls) }),
              el('td', { text: u.prompt.toLocaleString() }),
              el('td', { text: u.completion.toLocaleString() }),
              el('td', { text: u.total.toLocaleString() }),
            ));
          }
          tbl.append(tbody);
          usageBox.append(tbl);
        }
        // By model summary
        const byModel = usage.by_model || {};
        const modelRows = Object.entries(byModel)
          .sort((a, b) => b[1].total - a[1].total);
        if (modelRows.length) {
          usageBox.append(el('p', { class: 'muted small',
            text: 'By model: ' + modelRows
              .map(([m, u]) => `${m} (${u.total.toLocaleString()})`)
              .join(', ') }));
        }
        note.append(usageBox);
      }).catch(() => { /* usage is non-essential */ });
    }
  }).catch(() => { /* overview stats are non-essential */ });
}

async function retryRun() {
  await api(`/api/runs/${state.runId}/advance`, { method: 'POST' });
  toast('Queued for another attempt.', 'ok');
  await refreshStatus();
}

/* ── re-running a step ───────────────────────────────────────────────── */

function confirmRerun(stepName) {
  api(`/api/runs/${state.runId}/steps/${stepName}/impact`).then(impact => {
    const discarding = impact.cascade.filter(c => c.will_discard);
    const body = el('div', {},
      el('p', {}, 'Re-running ',
        el('strong', { text: stepName }),
        discarding.length
          ? ` discards the output of ${discarding.length} completed step(s):`
          : ' — nothing completed will be discarded.'),
      discarding.length
        ? el('div', { class: 'directive-preview',
                      text: discarding.map(c => `− ${c.label}`).join('\n') })
        : null,
      el('p', { class: 'muted small' },
        'Everything before this step is kept, including breaks you have already answered.'),
    );
    openModal('Re-run step', body, async () => {
      await api(`/api/runs/${state.runId}/steps/${stepName}/rerun`, {
        method: 'POST', body: { cascade: true },
      });
      toast(`Re-running from ${stepName}.`, 'ok');
      state.tab = 'overview';
      switchTab('overview');
      await refreshStatus();
      startPolling();
    });
  }).catch(err => toast(err.message, 'error'));
}

function openModal(title, bodyNode, onConfirm) {
  $('#modal-title').textContent = title;
  clear($('#modal-body'));
  $('#modal-body').append(bodyNode);
  $('#modal').hidden = false;

  const confirm = $('#modal-confirm');
  const fresh = confirm.cloneNode(true);
  confirm.replaceWith(fresh);
  fresh.addEventListener('click', async () => {
    fresh.disabled = true;
    try { await onConfirm(); }
    catch (err) { toast(err.message, 'error'); }
    finally { fresh.disabled = false; $('#modal').hidden = true; }
  });
}

/* ── break screens ───────────────────────────────────────────────────── */

async function openBreak(breakNum) {
  const payload = await api(`/api/runs/${state.runId}/break/${breakNum}`);
  state.breakDraft = {
    breakNum,
    payload,
    removedThemes: new Set(),
    addedThemes: new Set(),
    removedGaps: new Set(),
    correctedGaps: new Map(),
    newGaps: [],
    seminalOverrides: new Map(),
    verdictOverrides: new Map(),
    outputs: [],
    freeText: '',
  };
  switchTab('break');
  renderBreak();
}

function renderBreak() {
  const draft = state.breakDraft;
  const panel = $('#panel-break');
  clear(panel);
  if (!draft) return;

  const { payload, breakNum } = draft;

  panel.append(el('div', { class: 'break-header' },
    el('h2', { text: payload.title }),
    el('p', { class: 'small muted', text: payload.is_current
      ? 'The pipeline is paused here. Nothing runs until you respond.'
      : 'Already answered. Re-run this break step to change your response.' }),
  ));

  if (breakNum === 0) renderBreak0(panel, draft);
  if (breakNum === 1) renderBreak1(panel, draft);
  if (breakNum === 2) renderBreak2(panel, draft);

  // Models for agents that have not run yet — a break is the safe moment
  const completed = (state.status ? state.status.steps : [])
    .filter(s => ['done', 'skipped'].includes(s.status)).map(s => s.name);
  const modelBox = el('div', { class: 'model-grid' });
  panel.append(el('details', { class: 'disclosure' },
    el('summary', {}, 'Change models for the agents still to come'),
    el('p', { class: 'muted small' },
      'Applies to this run only, and only to agents that have not run yet.'),
    modelBox,
  ));
  buildModelGrid(modelBox, { completed, scope: `break${breakNum}` });

  panel.append(el('div', { class: 'review-group' },
    el('h3', {}, 'Anything else'),
    el('textarea', {
      id: 'break-free-text', rows: 3,
      placeholder: 'Free-form guidance for the agents that come next…',
      onInput: ev => { draft.freeText = ev.target.value; updatePreview(); },
    }),
  ));

  panel.append(el('div', { class: 'review-group' },
    el('h3', {}, 'What will be submitted'),
    el('p', { class: 'muted small' },
      'Your choices above become these instructions — the same ones the CLI accepts.'),
    el('div', { class: 'directive-preview', id: 'directive-preview' }),
  ));

  const submit = el('button', {
    class: 'btn btn-primary', type: 'button', id: 'btn-submit-break',
    disabled: !payload.is_current,
    onClick: submitBreak,
  }, payload.is_current ? 'Submit and continue' : 'Already answered');
  panel.append(submit);

  updatePreview();
}

function renderBreak0(panel, draft) {
  const themes = draft.payload.fields.themes || [];
  const group = el('div', { class: 'review-group' },
    el('h3', {},
      'Themes to search',
      el('span', { class: 'muted small', id: 'theme-count' })),
    el('p', { class: 'muted small' },
      'The concept mapper activated these from your problem. Add or remove any before searching begins.',
      el('span', { class: 'muted small', style: 'display:block;margin-top:4px;' },
        'Use Preview to confirm a theme has live coverage before committing to it (2 sources, 3 results each).'),
    ),
  );

  const grid = el('div', { class: 'theme-grid' });
  for (const theme of themes) {
    const checkbox = el('input', {
      type: 'checkbox', checked: theme.selected,
      onChange: ev => {
        const on = ev.target.checked;
        if (theme.selected && !on) draft.removedThemes.add(theme.theme_id);
        else draft.removedThemes.delete(theme.theme_id);
        if (!theme.selected && on) draft.addedThemes.add(theme.theme_id);
        else draft.addedThemes.delete(theme.theme_id);
        updatePreview();
      },
    });

    // Preview button (F1) — fires a lightweight OpenAlex + Semantic Scholar
    // probe and shows the top titles inline so the researcher can confirm
    // coverage before the full run commits to the theme.
    const previewBody = el('div', { class: 'theme-preview-body' });
    const previewBtn = el('button', {
      type: 'button', class: 'btn btn-ghost btn-small theme-preview-btn',
      onClick: async () => {
        clear(previewBody);
        previewBody.append(el('span', { class: 'muted small', text: 'Searching…' }));
        previewBtn.disabled = true;
        try {
          const result = await api(
            `/api/runs/${state.runId}/break/0/preview?theme=${encodeURIComponent(theme.theme_id)}`,
            { method: 'POST' },
          );
          clear(previewBody);
          if (!result.results || !result.results.length) {
            previewBody.append(el('span', { class: 'muted small',
              text: `No results from ${result.sources_hit}/2 sources — this theme may have thin coverage.` }));
          } else {
            previewBody.append(el('span', { class: 'muted small',
              text: `${result.total} results from ${result.sources_hit}/2 sources:` }));
            const list = el('ul', { class: 'theme-preview-list' });
            for (const r of result.results) {
              list.append(el('li', {},
                el('span', { class: 'theme-preview-source', text: r.source }),
                el('span', { class: 'theme-preview-title', text: r.title }),
                el('span', { class: 'muted small',
                  text: `${(r.authors || []).slice(0, 2).join(', ')}${r.authors && r.authors.length ? ' ' : ''}${r.year ? `(${r.year})` : ''}` }),
              ));
            }
            previewBody.append(list);
          }
        } catch (err) {
          clear(previewBody);
          previewBody.append(el('span', { class: 'muted small',
            text: `Preview failed: ${err.message}` }));
        } finally {
          previewBtn.disabled = false;
        }
      },
    }, 'Preview');

    grid.append(el('label', { class: 'theme-chip' }, checkbox,
      el('span', {},
        el('div', { class: 'theme-chip-name', text: theme.label || theme.theme_id }),
        el('div', { class: 'theme-chip-kw',
                    text: (theme.keywords || []).slice(0, 4).join(', ') }),
      ),
      el('div', { class: 'theme-chip-actions' }, previewBtn),
      previewBody,
    ));
  }
  group.append(grid);

  // "Add a theme" widget (review U6) — lets the researcher add a theme the
  // concept mapper missed without typing "ADD THEME:" in free-text.
  const addedList = el('div', { class: 'added-themes-list' });
  const rerenderAdded = () => {
    clear(addedList);
    for (const t of draft.addedThemes) {
      if (themes.some(th => th.theme_id === t)) continue; // skip existing toggles
      addedList.append(el('span', { class: 'theme-chip added-theme-chip' },
        el('span', { class: 'theme-chip-name', text: t }),
        el('button', { class: 'btn btn-ghost btn-small', type: 'button',
          onClick: () => { draft.addedThemes.delete(t); rerenderAdded(); updatePreview(); },
        }, '×'),
      ));
    }
  };
  const doAdd = () => {
    const name = themeInput.value.trim();
    if (!name) return;
    draft.addedThemes.add(name);
    themeInput.value = '';
    rerenderAdded();
    updatePreview();
  };
  const themeInput = el('input', {
    type: 'text', placeholder: 'Add a theme the concept mapper missed…',
    onKeyDown: ev => { if (ev.key === 'Enter') { ev.preventDefault(); doAdd(); } },
  });
  group.append(el('div', { class: 'add-theme-row' },
    themeInput,
    el('button', { class: 'btn btn-small', type: 'button',
                   onClick: doAdd }, 'Add theme'),
  ));
  group.append(addedList);

  panel.append(group);
}

function renderBreak1(panel, draft) {
  const { gaps = [], seminal = [], historical = [] } = draft.payload.fields;

  const gapGroup = el('div', { class: 'review-group' },
    el('h3', {}, 'Gaps identified',
      el('span', { class: 'muted small', text: `${gaps.length} found` })),
    el('p', { class: 'muted small' },
      'Correct anything the pipeline got wrong. Removed gaps are not used downstream.'),
  );

  for (const gap of gaps) {
    const id = gap.gap_id;
    const item = el('div', { class: 'item' });
    const rerender = () => item.classList.toggle('is-removed', draft.removedGaps.has(id));

    item.append(
      el('div', { class: 'item-title', text: gap.description || '' }),
      el('div', { class: 'item-meta',
                  text: `${id} · ${gap.significance || '—'} · ${gap.gap_type || '—'}` }),
      el('div', { class: 'item-actions' },
        el('button', { class: 'btn btn-small', type: 'button',
          onClick: () => {
            if (draft.removedGaps.has(id)) draft.removedGaps.delete(id);
            else { draft.removedGaps.add(id); draft.correctedGaps.delete(id); }
            rerender(); updatePreview();
          },
        }, 'Remove / restore'),
        el('button', { class: 'btn btn-small', type: 'button',
          onClick: () => {
            const box = $('.gap-correction', item);
            box.hidden = !box.hidden;
            if (!box.hidden) box.focus();
          },
        }, 'Correct'),
      ),
      el('textarea', {
        class: 'gap-correction', rows: 2, hidden: true,
        placeholder: 'What should this gap actually say?',
        onInput: ev => {
          const value = ev.target.value.trim();
          if (value) draft.correctedGaps.set(id, value);
          else draft.correctedGaps.delete(id);
          updatePreview();
        },
      }),
    );
    gapGroup.append(item);
  }

  const newGapInput = el('textarea', { rows: 2,
    placeholder: 'A gap the pipeline missed…' });
  gapGroup.append(el('div', { class: 'item' },
    el('div', { class: 'item-title', text: 'Add a gap' }),
    newGapInput,
    el('div', { class: 'item-actions' },
      el('button', { class: 'btn btn-small', type: 'button',
        onClick: () => {
          const value = newGapInput.value.trim();
          if (!value) return;
          draft.newGaps.push(value);
          newGapInput.value = '';
          toast('Gap added to your instructions.', 'ok');
          updatePreview();
        },
      }, 'Add'),
    ),
  ));
  panel.append(gapGroup);

  if (seminal.length) {
    const group = el('div', { class: 'review-group' },
      el('h3', {}, 'Seminal works',
        el('span', { class: 'muted small', text: `${seminal.length} found` })),
      el('p', { class: 'muted small' },
        'Disagree with why something was called seminal? Note it here.'),
    );
    for (const source of seminal.slice(0, 30)) {
      const id = source.source_id;
      group.append(el('div', { class: 'item' },
        el('div', { class: 'item-title',
                    text: `${source.year || 'n.d.'} — ${source.title || ''}` }),
        el('div', { class: 'item-meta', text: source.seminal_reason || '' }),
        el('textarea', { rows: 1, placeholder: 'Override this assessment…',
          onInput: ev => {
            const value = ev.target.value.trim();
            if (value) draft.seminalOverrides.set(id, value);
            else draft.seminalOverrides.delete(id);
            updatePreview();
          },
        }),
      ));
    }
    panel.append(group);
  }

  if (historical.length) {
    panel.append(el('div', { class: 'review-group' },
      el('h3', {}, 'Historical map',
        el('span', { class: 'muted small', text: `${historical.length} entries` })),
      el('div', { class: 'directive-preview', text: historical.slice(0, 25)
        .map(s => `${s.year || 'n.d.'}  ${s.title || ''}`).join('\n') }),
    ));
  }
}

function renderBreak2(panel, draft) {
  const { synthesis = {}, evaluations = [], output_types = [] } = draft.payload.fields;

  if (synthesis.sharpened_problem) {
    panel.append(el('div', { class: 'review-group' },
      el('h3', {}, 'Sharpened problem'),
      el('div', { class: 'narrative', text: synthesis.sharpened_problem }),
    ));
  }
  if (synthesis.full_narrative) {
    panel.append(el('div', { class: 'review-group' },
      el('h3', {}, 'Research narrative'),
      el('div', { class: 'narrative', text: synthesis.full_narrative }),
    ));
  }
  if (synthesis.trajectory_statement) {
    panel.append(el('div', { class: 'review-group' },
      el('h3', {}, 'Trajectory'),
      el('div', { class: 'narrative', text: synthesis.trajectory_statement }),
    ));
  }

  if (evaluations.length) {
    const group = el('div', { class: 'review-group' },
      el('h3', {}, 'Feasibility verdicts',
        el('span', { class: 'muted small', text: `${evaluations.length} evaluated` })),
      el('p', { class: 'muted small' },
        'Rude is adversarial by design. Override any verdict you disagree with.'),
    );
    for (const evaluation of evaluations) {
      const id = evaluation.evaluation_id;
      group.append(el('div', { class: 'item' },
        el('div', { class: 'item-title' },
          el('span', { class: `verdict-${evaluation.verdict || ''}`,
                       text: (evaluation.verdict || '').replace(/_/g, ' ') || 'unrated' }),
          ' — ', (evaluation.proposal_text || '').slice(0, 180)),
        el('div', { class: 'item-meta', text: evaluation.verdict_reason || '' }),
        el('textarea', { rows: 2, placeholder: 'Why is this verdict wrong?',
          onInput: ev => {
            const value = ev.target.value.trim();
            if (value) draft.verdictOverrides.set(id, value);
            else draft.verdictOverrides.delete(id);
            updatePreview();
          },
        }),
      ));
    }
    panel.append(group);
  }

  const typeSelect = el('select', {}, output_types.map(
    t => el('option', { value: t, text: t.replace(/_/g, ' ') })));
  const audienceInput = el('input', { type: 'text', value: 'researcher',
                                      placeholder: 'audience' });
  const chosen = el('div');

  const renderChosen = () => {
    clear(chosen);
    draft.outputs.forEach((output, index) => {
      chosen.append(el('div', { class: 'item' },
        el('div', { class: 'item-title',
                    text: `${output.type} — for ${output.audience}` }),
        el('div', { class: 'item-actions' },
          el('button', { class: 'btn btn-small', type: 'button',
            onClick: () => { draft.outputs.splice(index, 1); renderChosen(); updatePreview(); },
          }, 'Remove'),
        ),
      ));
    });
  };

  panel.append(el('div', { class: 'review-group' },
    el('h3', {}, 'Outputs to produce'),
    el('p', { class: 'muted small' },
      'The Understanding Map is always produced. Request anything else here.'),
    chosen,
    el('div', { class: 'item' },
      el('div', { class: 'model-grid' },
        el('div', { class: 'model-row' }, el('label', {}, 'Type'), typeSelect),
        el('div', { class: 'model-row' }, el('label', {}, 'Audience'), audienceInput),
      ),
      el('div', { class: 'item-actions' },
        el('button', { class: 'btn btn-small', type: 'button',
          onClick: () => {
            draft.outputs.push({
              type: typeSelect.value,
              audience: audienceInput.value.trim() || 'researcher',
            });
            renderChosen(); updatePreview();
          },
        }, 'Add output'),
      ),
    ),
  ));
}

/* The single place widgets become directives. */
function buildDirectives(draft) {
  const lines = [];
  for (const id of draft.removedThemes) lines.push(`REMOVE THEME: ${id}`);
  for (const id of draft.addedThemes)   lines.push(`ADD THEME: ${id}`);
  for (const id of draft.removedGaps)   lines.push(`REMOVE GAP ${id}`);
  for (const [id, text] of draft.correctedGaps) lines.push(`CORRECT GAP ${id}: ${text}`);
  for (const text of draft.newGaps)     lines.push(`ADD GAP: ${text}`);
  for (const [id, text] of draft.seminalOverrides) lines.push(`OVERRIDE SEMINAL ${id}: ${text}`);
  for (const [id, text] of draft.verdictOverrides) lines.push(`OVERRIDE VERDICT ${id}: ${text}`);
  for (const output of draft.outputs) {
    lines.push(`SCRIBE OUTPUT: ${output.type} | audience: ${output.audience}`);
  }
  if (!lines.length && !draft.freeText.trim()) lines.push('CONFIRMED');
  return lines;
}

function updatePreview() {
  const draft = state.breakDraft;
  const node = $('#directive-preview');
  if (!draft || !node) return;
  const lines = buildDirectives(draft);
  const text = lines.concat(draft.freeText.trim() ? [draft.freeText.trim()] : []).join('\n');
  node.textContent = text;

  const count = $('#theme-count');
  if (count && draft.payload.fields.themes) {
    const total = draft.payload.fields.themes
      .filter(t => (t.selected && !draft.removedThemes.has(t.theme_id)) ||
                   draft.addedThemes.has(t.theme_id)).length;
    count.textContent = `${total} selected`;
  }
}

async function submitBreak() {
  const draft = state.breakDraft;
  if (!draft) return;
  const button = $('#btn-submit-break');
  button.disabled = true;
  button.textContent = 'Submitting…';

  try {
    await api(`/api/runs/${state.runId}/break/${draft.breakNum}`, {
      method: 'POST',
      body: {
        directives: buildDirectives(draft),
        instructions: draft.freeText.trim(),
        model_overrides: collectModelOverrides($('#panel-break')),
        source_overrides: collectSourceOverrides($('#panel-break')),
      },
    });
    toast('Submitted — the pipeline is moving again.', 'ok');
    state.breakDraft = null;
    switchTab('overview');
    await refreshStatus();
    startPolling();
  } catch (err) {
    toast(err.message, 'error');
    button.disabled = false;
    button.textContent = 'Submit and continue';
  }
}

/* ── artifacts ───────────────────────────────────────────────────────── */

async function renderArtifacts() {
  const panel = $('#panel-artifacts');
  clear(panel);

  // Fetch Scribe artifacts and per-step artifacts in parallel (F6).
  const [scribeRes, stepRes] = await Promise.all([
    api(`/api/runs/${state.runId}/artifacts`).catch(() => ({ artifacts: [] })),
    api(`/api/runs/${state.runId}/step-artifacts`).catch(() => ({ files: [] })),
  ]);
  const artifacts = scribeRes.artifacts || [];
  const stepFiles = stepRes.files || [];

  if (!artifacts.length && !stepFiles.length) {
    panel.append(el('p', { class: 'empty',
      text: 'No artifacts yet. The pipeline writes them as each step completes.' }));
    return;
  }

  // Scribe's curated artifacts (the original Artifacts tab content).
  if (artifacts.length) {
    panel.append(el('h3', {}, 'Scribe artifacts'),
      el('p', { class: 'muted small' },
        'Final outputs produced by Scribe at the end of the run.'));
    for (const artifact of artifacts) {
      const body = el('div', { hidden: true });
      panel.append(el('div', { class: 'item' },
        el('div', { class: 'item-title',
                    text: artifact.title || artifact.output_type }),
        el('div', { class: 'item-meta',
          text: [artifact.output_type, artifact.audience,
                 artifact.word_count ? `${artifact.word_count} words` : null,
                 shortTime(artifact.date_produced)].filter(Boolean).join(' · ') }),
        el('div', { class: 'item-actions' },
          el('button', { class: 'btn btn-small', type: 'button',
            onClick: async ev => {
              if (!body.hidden) { body.hidden = true; ev.target.textContent = 'View'; return; }
              ev.target.disabled = true;
              try {
                const full = await api(
                  `/api/runs/${state.runId}/artifacts/${artifact.artifact_id}`);
                clear(body);
                body.append(el('div', { class: 'narrative',
                  text: full.content || '(the file is missing on disk)' }));
                body.hidden = false;
                ev.target.textContent = 'Hide';
              } catch (err) {
                toast(err.message, 'error');
              } finally { ev.target.disabled = false; }
            },
          }, 'View'),
        ),
        body,
      ));
    }
  }

  // Per-step artifacts (F6) — every agent's markdown doc.
  if (stepFiles.length) {
    if (artifacts.length) panel.append(el('hr', {}));
    panel.append(el('h3', {}, 'Per-step documents'),
      el('p', { class: 'muted small' },
        'Each agent writes a markdown document as it runs. These are the ' +
        'raw outputs behind the Understanding Map — useful for tracing ' +
        'how a claim entered the synthesis.'));
    for (const f of stepFiles) {
      const body = el('div', { hidden: true });
      panel.append(el('div', { class: 'item' },
        el('div', { class: 'item-title', text: f.label }),
        el('div', { class: 'item-meta',
          text: [`${(f.size / 1024).toFixed(1)} KB`, shortTime(f.modified)]
            .filter(Boolean).join(' · ') }),
        el('div', { class: 'item-actions' },
          el('button', { class: 'btn btn-small', type: 'button',
            onClick: async ev => {
              if (!body.hidden) { body.hidden = true; ev.target.textContent = 'View'; return; }
              ev.target.disabled = true;
              try {
                const full = await api(
                  `/api/runs/${state.runId}/step-artifacts/${encodeURIComponent(f.filename)}`);
                clear(body);
                body.append(el('div', { class: 'narrative',
                  text: full.content || '(empty)' }));
                body.hidden = false;
                ev.target.textContent = 'Hide';
              } catch (err) {
                toast(err.message, 'error');
              } finally { ev.target.disabled = false; }
            },
          }, 'View'),
        ),
        body,
      ));
    }
  }
}

/* ── tabs & wiring ───────────────────────────────────────────────────── */

/*
 * Argument tree visualization (F9).
 * Renders the run's argument tree as a collapsible nested list. Nodes are
 * color-coded by type and audit status. Clicking a node shows its metadata
 * and source.
 */
const TREE_NODE_COLORS = {
  root:       'tree-node-root',
  question:   'tree-node-question',
  claim:      'tree-node-claim',
  evidence:   'tree-node-evidence',
  bridge:     'tree-node-bridge',
  counter:    'tree-node-counter',
  historical: 'tree-node-historical',
  external:   'tree-node-external',
  audit_note: 'tree-node-audit',
};
const TREE_STATUS_BADGE = {
  solid:        '✓ solid',
  supported:    '✓ supported',
  contested:    '⚠ contested',
  contradicted: '✗ contradicted',
  weak:         '⚠ weak',
  unsupported:  '? unsupported',
  bridged:      '⇄ bridged',
};
const TREE_TYPE_LABEL = {
  root: 'Root', question: 'Question', claim: 'Claim', evidence: 'Evidence',
  bridge: 'Bridge', counter: 'Counter', historical: 'Historical',
  external: 'External', audit_note: 'Audit',
};

async function renderTree() {
  const panel = $('#panel-tree');
  if (!panel) return;
  clear(panel);
  panel.append(el('p', { class: 'muted small', text: 'Loading argument tree…' }));

  let data;
  try {
    data = await api(`/api/runs/${state.runId}/tree`);
  } catch (err) {
    clear(panel);
    panel.append(el('p', { class: 'empty', text: `Failed to load tree: ${err.message}` }));
    return;
  }

  clear(panel);
  const { tree, stats, sources } = data;

  if (!tree || !tree.node_id) {
    panel.append(el('p', { class: 'empty',
      text: 'No argument tree yet. The tree grows as Grounder, Social, and Historian run.' }));
    return;
  }

  // Stats summary
  const statRow = el('div', { class: 'tree-stats' });
  const byType = stats.by_type || {};
  for (const t of ['question', 'claim', 'evidence', 'counter', 'bridge', 'historical', 'external', 'audit_note']) {
    if (byType[t]) {
      statRow.append(el('span', { class: 'tree-stat-chip',
        text: `${byType[t]} ${TREE_TYPE_LABEL[t] || t}` }));
    }
  }
  if (stats.unique_sources) {
    statRow.append(el('span', { class: 'tree-stat-chip',
      text: `${stats.unique_sources} sources` }));
  }
  const claimStatuses = stats.claim_statuses || {};
  for (const [s, n] of Object.entries(claimStatuses)) {
    if (TREE_STATUS_BADGE[s]) {
      statRow.append(el('span', { class: 'tree-stat-chip tree-stat-status',
        text: `${n} ${TREE_STATUS_BADGE[s]}` }));
    }
  }

  panel.append(el('div', { class: 'review-group' },
    el('h3', {}, 'Argument Tree'),
    el('p', { class: 'muted small' },
      'Every claim traces to evidence. Click a node to inspect its source and metadata. ' +
      'Nodes are color-coded by type; claims carry an audit status badge.'),
    statRow,
  ));

  // Legend
  const legend = el('div', { class: 'tree-legend' });
  for (const [t, cls] of Object.entries(TREE_NODE_COLORS)) {
    legend.append(el('span', { class: `tree-legend-item ${cls}`,
      text: TREE_TYPE_LABEL[t] || t }));
  }
  panel.append(legend);

  // Detail panel (shown when a node is clicked)
  const detail = el('div', { class: 'tree-detail', id: 'tree-detail' });
  panel.append(detail);

  // Recursive tree renderer
  const renderNode = (node, depth = 0) => {
    if (!node) return null;
    const type = node.node_type || 'unknown';
    const colorClass = TREE_NODE_COLORS[type] || '';
    const hasChildren = node.children && node.children.length > 0;

    const childContainer = el('div', { class: 'tree-children', hidden: depth > 1 });

    const toggle = hasChildren
      ? el('span', {
          class: 'tree-toggle',
          onClick: ev => {
            ev.stopPropagation();
            childContainer.hidden = !childContainer.hidden;
            toggle.textContent = childContainer.hidden ? '▸' : '▾';
          },
        }, depth > 1 ? '▸' : '▾')
      : el('span', { class: 'tree-toggle tree-toggle-leaf' }, '·');

    const statusBadge = (type === 'claim' && node.status && TREE_STATUS_BADGE[node.status])
      ? el('span', { class: `tree-status tree-status-${node.status}`,
                     text: TREE_STATUS_BADGE[node.status] })
      : null;

    const confidenceBadge = (type === 'claim' && node.confidence != null && node.confidence > 0)
      ? el('span', { class: 'tree-confidence',
                     text: `${Math.round(node.confidence * 100)}%` })
      : null;

    const typeLabel = el('span', { class: `tree-type-label ${colorClass}`,
      text: TREE_TYPE_LABEL[type] || type });

    const content = (node.content || '').slice(0, 200);

    const nodeEl = el('div', {
      class: `tree-node ${colorClass}`,
      onClick: () => showNodeDetail(node, sources, detail),
    },
      toggle,
      typeLabel,
      el('span', { class: 'tree-node-content', text: content }),
      statusBadge,
      confidenceBadge,
    );

    const wrapper = el('div', { class: 'tree-node-wrapper' }, nodeEl, childContainer);

    if (hasChildren) {
      for (const child of node.children) {
        const childEl = renderNode(child, depth + 1);
        if (childEl) childContainer.append(childEl);
      }
    }

    return wrapper;
  };

  const treeRoot = renderNode(tree, 0);
  if (treeRoot) panel.append(treeRoot);
}

function showNodeDetail(node, sources, container) {
  clear(container);
  const type = node.node_type || 'unknown';
  const meta = node.metadata || {};

  const rows = [
    ['Type', TREE_TYPE_LABEL[type] || type],
    ['Status', node.status || '—'],
    ['Confidence', node.confidence != null ? `${Math.round(node.confidence * 100)}%` : '—'],
    ['Agent', node.agent_origin || '—'],
    ['Created', shortTime(node.created_at)],
  ];

  if (type === 'evidence') {
    if (meta.evidence_type) rows.push(['Evidence type', meta.evidence_type]);
    if (meta.relationship) rows.push(['Relationship', meta.relationship]);
    if (meta.snippet) rows.push(['Snippet', meta.snippet]);
  }
  if (type === 'historical' && meta.year) rows.push(['Year', String(meta.year)]);
  if (type === 'external' && meta.factor_type) rows.push(['Factor type', meta.factor_type]);
  if (type === 'bridge' && meta.bridge_type) rows.push(['Bridge type', meta.bridge_type]);

  const table = el('table', { class: 'tree-detail-table' });
  for (const [k, v] of rows) {
    table.append(el('tr', {},
      el('th', { text: k }),
      el('td', { text: v }),
    ));
  }

  container.append(
    el('h4', {}, 'Node detail'),
    table,
  );

  // Content (full)
  if (node.content && node.content.length > 200) {
    container.append(el('div', { class: 'tree-detail-content' },
      el('strong', {}, 'Full content:'),
      el('p', { text: node.content }),
    ));
  }

  // Sources
  const sourceIds = node.source_ids || [];
  if (sourceIds.length) {
    const srcList = el('ul', { class: 'tree-detail-sources' });
    for (const sid of sourceIds) {
      const src = sources[sid];
      if (src) {
        const link = src.active_link
          ? el('a', { href: src.active_link, target: '_blank', rel: 'noopener',
                      text: src.title || sid })
          : el('span', { text: src.title || sid });
        srcList.append(el('li', {},
          el('span', { class: 'tree-source-name', text: src.source_name || '' }),
          ' ',
          link,
          src.year ? el('span', { class: 'muted small', text: ` (${src.year})` }) : null,
        ));
      } else {
        srcList.append(el('li', { class: 'muted small', text: `${sid} (source not found)` }));
      }
    }
    container.append(el('div', {},
      el('strong', {}, 'Sources:'),
      srcList,
    ));
  }
}

function switchTab(name) {
  state.tab = name;
  $$('.tab').forEach(t => t.classList.toggle('is-active', t.dataset.tab === name));
  $('#panel-overview').hidden  = name !== 'overview';
  $('#panel-break').hidden     = name !== 'break';
  $('#panel-sources').hidden   = name !== 'sources';
  $('#panel-tree').hidden      = name !== 'tree';
  $('#panel-artifacts').hidden = name !== 'artifacts';

  if (name === 'artifacts') renderArtifacts().catch(err => toast(err.message, 'error'));
  if (name === 'overview' && state.status) renderOverview(state.status);
  if (name === 'sources') renderSources().catch(err => toast(err.message, 'error'));
  if (name === 'tree') renderTree().catch(err => toast(err.message, 'error'));
  if (name === 'break' && !state.breakDraft && state.status &&
      state.status.awaiting_break !== null) {
    openBreak(state.status.awaiting_break).catch(err => toast(err.message, 'error'));
  }
}

// Per-run source health + coverage (review U3). Shows which sources
// succeeded / failed / were skipped, how many results each returned, and
// how many made it into the Understanding Map.
async function renderSources() {
  const panel = $('#panel-sources');
  if (!panel) return;
  clear(panel);
  panel.append(el('p', { class: 'muted small', text: 'Loading source health…' }));
  let data;
  try {
    data = await api(`/api/runs/${state.runId}/sources`);
  } catch (err) {
    clear(panel);
    panel.append(el('p', { class: 'muted small', text: err.message }));
    return;
  }
  clear(panel);
  const health = data.health || [];
  const inserted = data.inserted || {};
  const previouslySeen = data.previously_seen || 0;
  const previousRunId = data.previous_run_id || '';
  if (!health.length) {
    panel.append(el('p', { class: 'muted small',
      text: 'No source activity yet — the gathering steps have not run.' }));
    return;
  }
  // F5: previously-seen banner
  if (previouslySeen > 0 && previousRunId) {
    panel.append(el('div', { class: 'review-group' },
      el('h3', {}, 'Cross-run comparison'),
      el('p', { class: 'muted small' },
        `${previouslySeen} source(s) in this run also appeared in the previous run `,
        el('code', { text: previousRunId.slice(-8) }),
        `. These are flagged as "previously seen" in the Understanding Map. ` +
        `${Object.values(inserted).reduce((a,b)=>a+b,0) - previouslySeen} source(s) are new.`),
    ));
  }
  panel.append(el('h3', {}, 'Source coverage'),
    el('p', { class: 'muted small',
      text: 'Each source the gathering steps queried, with its outcome and '
          + 'how many results made it into the Understanding Map.' }),
    el('table', { class: 'source-table' },
      el('thead', {},
        el('tr', {},
          el('th', { text: 'Source' }),
          el('th', { text: 'Step' }),
          el('th', { text: 'Status' }),
          el('th', { text: 'Results' }),
          el('th', { text: 'Inserted' }),
          el('th', { text: 'Last error' }),
        ),
      ),
      el('tbody', {},
        ...health.map(h => el('tr', {},
          el('td', { text: h.source_id }),
          el('td', { text: h.step || '' }),
          el('td', { class: `src-status is-${h.status}`, text: h.status }),
          el('td', { text: h.results_returned ?? '' }),
          el('td', { text: inserted[h.source_id] ?? 0 }),
          el('td', { class: 'muted small', text: (h.last_error || '').slice(0, 80) }),
        )),
      ),
    ),
  );
}

function wireChrome() {
  $('#run-tabs').addEventListener('click', ev => {
    const tab = ev.target.closest('.tab');
    if (tab) switchTab(tab.dataset.tab);
  });
  $$('[data-nav="runs"]').forEach(node => {
    node.addEventListener('click', () => showRuns().catch(err => toast(err.message, 'error')));
  });
  $('#modal-cancel').addEventListener('click', () => { $('#modal').hidden = true; });
  $('#modal').addEventListener('click', ev => {
    if (ev.target.id === 'modal') $('#modal').hidden = true;
  });
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') $('#modal').hidden = true;
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling();
    else if (state.runId && !$('#view-run').hidden) startPolling();
  });
}

/* ── start ───────────────────────────────────────────────────────────── */

async function init() {
  wireLogin();
  wireNewRun();
  wireChrome();

  try {
    const health = await api('/api/health');
    $('#storage-badge').textContent = health.storage;
    if (!health.secrets_configured) {
      toast('Server has no SEEKER_SECRET_KEY — sign-in will be refused.', 'error');
    }
  } catch { /* health is advisory */ }

  try {
    await afterSignIn();          // an existing session cookie signs us straight in
  } catch {
    showView('login');
  }
}

document.addEventListener('DOMContentLoaded', init);
