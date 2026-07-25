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

function showNewRun() {
  showView('new');
  const providerSelect = $('#new-provider');
  clear(providerSelect);
  for (const cred of state.providers) {
    providerSelect.append(el('option', { value: cred.provider, text: cred.provider }));
  }
  buildModelGrid($('#new-model-grid'));
  $('#new-error').hidden = true;
}

function wireNewRun() {
  $('#btn-new-run').addEventListener('click', showNewRun);

  $('#form-new-run').addEventListener('submit', async ev => {
    ev.preventDefault();
    const button = $('#btn-start-run');
    const error = $('#new-error');
    error.hidden = true;
    button.disabled = true;
    button.textContent = 'Starting…';

    try {
      const body = await api('/api/runs', {
        method: 'POST',
        body: {
          problem:  $('#new-problem').value.trim(),
          provider: $('#new-provider').value,
          model_overrides: collectModelOverrides($('#new-model-grid')),
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
}

async function refreshStatus() {
  const previous = state.status;
  const status = await api(`/api/runs/${state.runId}/status`);
  state.status = status;

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

  const live = status.running || status.queued;
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

function renderOverview(status) {
  const panel = $('#panel-overview');
  clear(panel);

  if (status.failed_steps.length) {
    const failed = status.steps.find(s => s.status === 'failed');
    panel.append(el('div', { class: 'break-header' },
      el('h2', { text: `Stopped at ${failed ? failed.label : status.failed_steps[0]}` }),
      el('p', { class: 'small', text: (failed && failed.error) || '' }),
      el('button', {
        class: 'btn btn-small', type: 'button',
        onClick: () => retryRun(),
      }, 'Retry this step'),
    ));
  } else if (status.awaiting_break !== null) {
    panel.append(el('div', { class: 'break-header' },
      el('h2', { text: `Break ${status.awaiting_break} — your turn` }),
      el('p', { class: 'small',
                text: 'The pipeline is paused. Review what it found, then set the direction.' }),
      el('button', {
        class: 'btn btn-primary btn-small', type: 'button',
        onClick: () => openBreak(status.awaiting_break),
      }, 'Review and respond'),
    ));
  } else if (status.complete) {
    panel.append(el('div', { class: 'break-header' },
      el('h2', { text: 'Pipeline complete' }),
      el('p', { class: 'small', text: 'Your outputs are under Artifacts.' }),
    ));
  }

  api(`/api/runs/${state.runId}`).then(detail => {
    if (state.tab !== 'overview' || state.runId !== detail.run_id) return;
    const counts = detail.counts || {};
    const grid = el('div', { class: 'stat-grid' });
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
    panel.append(grid);

    const overrides = detail.model_overrides || {};
    if (Object.keys(overrides).length) {
      panel.append(el('div', { class: 'review-group' },
        el('h3', {}, 'Model routing for this run'),
        el('div', { class: 'directive-preview',
          text: Object.entries(overrides)
            .map(([agent, spec]) => `${agent}: ${spec.model || spec.provider || ''}`)
            .join('\n') }),
      ));
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
  $('#panel-artifacts').hidden = name !== 'artifacts';

  if (name === 'artifacts') renderArtifacts().catch(err => toast(err.message, 'error'));
  if (name === 'overview' && state.status) renderOverview(state.status);
  if (name === 'break' && !state.breakDraft && state.status &&
      state.status.awaiting_break !== null) {
    openBreak(state.status.awaiting_break).catch(err => toast(err.message, 'error'));
  }
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
