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
    container.append(el('div', { class: `model-row ${done ? 'is-done' : ''}` },
      el('label', { for: id, text: `${agent.name}${done ? ' (already run)' : ''}` }),
      el('select', { id, 'data-agent': agent.name, disabled: done }, modelOptions()),
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
  $('#new-error').hidden = true;
  $('#role-warning').hidden = true;
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
}

function collectSourceOverrides(container) {
  const overrides = {};
  for (const cb of container.querySelectorAll('input[type=checkbox][data-source]')) {
    overrides[cb.dataset.source] = cb.checked;
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
    const row = el('li', {
      class: `rail-step status-${step.status} ${isBreak ? 'is-break' : ''} ` +
             `${step.name === status.current_step ? 'is-current' : ''}`,
      title: step.error || step.label,
    },
      el('span', { class: 'rail-step-icon' }, icon),
      el('span', { class: 'rail-step-label', text: step.label }),
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
        (services.length ? ` · uses ${services.join(', ')}` : '') }),
      el('div', { class: 'live-actions' },
        el('button', { class: 'btn btn-small', type: 'button', id: 'btn-stop',
                       onClick: stopRun },
          'Stop and change model')),
    ));
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
      'The concept mapper activated these from your problem. Add or remove any before searching begins.'),
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
    grid.append(el('label', { class: 'theme-chip' }, checkbox,
      el('span', {},
        el('div', { class: 'theme-chip-name', text: theme.label || theme.theme_id }),
        el('div', { class: 'theme-chip-kw',
                    text: (theme.keywords || []).slice(0, 4).join(', ') }),
      ),
    ));
  }
  group.append(grid);
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
  const { artifacts } = await api(`/api/runs/${state.runId}/artifacts`);

  if (!artifacts.length) {
    panel.append(el('p', { class: 'empty',
      text: 'No artifacts yet. Scribe produces them at the end of the run.' }));
    return;
  }

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

/* ── tabs & wiring ───────────────────────────────────────────────────── */

function switchTab(name) {
  state.tab = name;
  $$('.tab').forEach(t => t.classList.toggle('is-active', t.dataset.tab === name));
  $('#panel-overview').hidden  = name !== 'overview';
  $('#panel-break').hidden     = name !== 'break';
  $('#panel-sources').hidden   = name !== 'sources';
  $('#panel-artifacts').hidden = name !== 'artifacts';

  if (name === 'artifacts') renderArtifacts().catch(err => toast(err.message, 'error'));
  if (name === 'overview' && state.status) renderOverview(state.status);
  if (name === 'sources') renderSources().catch(err => toast(err.message, 'error'));
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
  if (!health.length) {
    panel.append(el('p', { class: 'muted small',
      text: 'No source activity yet — the gathering steps have not run.' }));
    return;
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
