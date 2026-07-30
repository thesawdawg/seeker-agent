import { api } from '../core/api-client.js?v=24';
import { $, $$, clear, el, shortTime } from '../core/dom.js?v=24';
import { on } from '../core/events.js?v=24';
import { showView } from '../core/navigation.js?v=24';
import {
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  state,
  updateState,
} from '../core/store.js?v=24';
import { openModal } from '../components/modal.js?v=24';
import { toast } from '../components/toast.js?v=24';
import { openBreak } from './breaks.js?v=24';
import { renderArtifacts, switchTab } from './results.js?v=24';

/* ── run detail ──────────────────────────────────────────────────────── */

export async function openRun(runId) {
  updateState({
    runId,
    tab: 'overview',
    breakDraft: null,
    activityLog: [],
    lastEventSeq: 0,
    _lastDetailSig: null,
  }, 'run:opened');
  showView('run');

  const detail = await api(`/api/runs/${runId}`);
  $('#run-problem').textContent = detail.problem;
  $('#run-meta').textContent = `${runId} · started ${shortTime(detail.created_at)}`;

  await refreshStatus();
  startPolling();
}

// F3: SSE event stream — replaces the 2s polling loop. Falls back to
// polling if EventSource is unavailable or the connection fails.
export function startPolling() {
  stopPolling();
  if (typeof EventSource !== 'undefined' && state.runId) {
    startSSE();
  } else {
    startPollingLoop();
  }
}

function startSSE() {
  const url = `/api/runs/${state.runId}/events`;
  let es;
  try {
    es = new EventSource(url);
  } catch (e) {
    // EventSource not supported — fall back to polling
    startPollingLoop();
    return;
  }
  updateState({ eventSource: es }, 'run-monitor:sse-opened');

  es.addEventListener('status', async (ev) => {
    try {
      const status = JSON.parse(ev.data);
      const previous = state.status;
      updateState({ status }, 'run:status-received');
      recordActivity(status);
      renderRail(status);
      renderTabs(status);
      if (state.tab === 'overview') renderOverview(status);
      await handleStatusTransitions(status, previous);
      const live = status.running || status.queued || status.status === 'cancelling';
      $('#live-dot').classList.toggle('is-live', live);
    } catch (e) { /* ignore parse errors */ }
  });

  es.addEventListener('done', (ev) => {
    // Run is complete — close the stream. The client won't reconnect
    // because we set readyState to CLOSED.
    es.close();
    updateState({ eventSource: null }, 'run-monitor:sse-completed');
  });

  es.addEventListener('error', (ev) => {
    // EventSource auto-reconnects, but if the connection keeps failing
    // (e.g. behind a proxy that doesn't support SSE), fall back to
    // polling after the first error.
    if (es.readyState === EventSource.CLOSED) {
      updateState({ eventSource: null }, 'run-monitor:sse-failed');
      startPollingLoop();
    }
    // If readyState is CONNECTING, the browser is retrying — let it.
  });
}

function startPollingLoop() {
  // The original polling loop — kept as SSE fallback (F3).
  const tick = async () => {
    if (!state.runId) return;
    try {
      const moving = await refreshStatus();
      updateState({
        pollTimer: setTimeout(tick, moving ? POLL_ACTIVE_MS : POLL_IDLE_MS),
      }, 'run-monitor:poll-scheduled');
    } catch {
      // Transient failure: keep the loop alive but ease off
      updateState({
        pollTimer: setTimeout(tick, POLL_IDLE_MS),
      }, 'run-monitor:poll-backed-off');
    }
  };
  updateState({
    pollTimer: setTimeout(tick, POLL_ACTIVE_MS),
  }, 'run-monitor:poll-started');
}

export function stopPolling() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  updateState({ pollTimer: null }, 'run-monitor:poll-stopped');
  if (state.eventSource) {
    state.eventSource.close();
    updateState({ eventSource: null }, 'run-monitor:sse-stopped');
  }
  stopElapsedTicker();
}

export async function refreshStatus() {
  const previous = state.status;
  const status = await api(`/api/runs/${state.runId}/status?since_seq=${state.lastEventSeq}`);
  updateState({ status }, 'run:status-refreshed');

  recordActivity(status);

  renderRail(status);
  renderTabs(status);
  if (state.tab === 'overview') renderOverview(status);

  await handleStatusTransitions(status, previous);

  const live = status.running || status.queued || status.status === 'cancelling';
  $('#live-dot').classList.toggle('is-live', live);
  return live;
}

// F3: Shared transition handling — called by both refreshStatus (polling
// fallback) and the SSE status event handler.
async function handleStatusTransitions(status, previous) {
  if (previous === undefined) previous = state.status;

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
}

// Plain-BMP glyphs only. U+23F8 PAUSE renders as tofu wherever the font has no
// coverage — and awaiting_input is the one state the user must actually notice.
const STEP_ICON = {
  pending: '·', running: '', awaiting_input: '❚❚',
  done: '✓', failed: '✗', skipped: '⊖',
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
      const rerunBtn = el('button', {
        class: 'rail-rerun', type: 'button', title: 'Re-run this step',
        onClick: ev => { ev.stopPropagation(); confirmRerun(step.name); },
      }, '↻');
      row.append(rerunBtn);
      // F11: Branch from here — only for completed (not failed) steps.
      if (step.status === 'done') {
        row.append(el('button', {
          class: 'rail-branch', type: 'button', title: 'Branch from here',
          onClick: ev => { ev.stopPropagation(); confirmBranch(step.name); },
        }, '⎇'));
      }
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
    // aria-live on the live card: during a twenty-minute run this is the one
    // thing a screen-reader user most needs, and without it the page appears
    // frozen. 'polite' so it waits for a pause rather than interrupting.
    el('div', { id: 'live-card', role: 'status', 'aria-live': 'polite' }),
    el('div', { id: 'stat-grid', class: 'stat-grid' }),
    el('div', { id: 'routing-note' }),
    el('div', { class: 'review-group' },
      el('h3', {},
        'Activity log',
        el('span', { class: 'muted small', id: 'activity-count' })),
      el('p', { class: 'muted small',
                text: 'Every service the pipeline contacts, newest last.' }),
      // Not a live region: it appends a line per source call and would
      // narrate the entire run. The live card above carries the summary.
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
/* What the evidence base actually looks like, not just how many rows landed.
 * Sources whose landing page did not answer are kept and flagged rather than
 * discarded, and sources the model could not rate are counted separately from
 * ones it rated Medium — both of those need to be visible to be worth
 * anything (review X3, V2, V5). */
function coverageLine(data) {
  const link = data.link_status || {};
  const rel = data.relevance || {};
  const total = Object.values(link).reduce((a, b) => a + b, 0);
  if (!total) return el('span');

  const bits = [];
  const high = rel.High || 0, medium = rel.Medium || 0, low = rel.Low || 0;
  if (high || medium || low) {
    bits.push(el('span', { class: 'src-ok',
      text: `${high} high · ${medium} medium · ${low} low` }));
  }
  if (rel.unrated) {
    bits.push(el('span', { class: 'src-warn',
      title: 'The model could not rate these — check the provider. They are '
           + 'stored unrated and rank last, not as "Medium".',
      text: `${rel.unrated} unrated` }));
  }
  if (link.unreachable) {
    bits.push(el('span', { class: 'src-warn',
      title: 'The landing page did not answer. These sources are kept and '
           + 'flagged, not discarded — a DOI resolves independently.',
      text: `${link.unreachable} unreachable link(s)` }));
  }
  if (link.dead) {
    bits.push(el('span', { class: 'src-warn',
      title: 'The publisher returned 404/410. Kept, with the DOI as the link.',
      text: `${link.dead} dead link(s)` }));
  }
  return el('div', { class: 'source-progress-summary' },
    el('span', { text: `${total} source(s) retained` }), ...bits);
}

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
    container.append(coverageLine(data));
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
                body: { [h.source_id]: false },
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
                text: `All ${status.progress.total} steps finished. Your outputs are under Artifacts.` }),
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
      latestPreviewLine(running.name),
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
 * Accumulate the granular step_events the server streams (F3/verbose),
 * each carrying a preview of the data actually sent or received, so the
 * page shows a full history of what happened rather than only the latest
 * one-line status per step. `state.lastEventSeq` is a cursor into that
 * feed — every request only asks for events newer than what's already
 * been rendered, so a long run doesn't re-transmit its whole history.
 */
function recordActivity(status) {
  const events = status.events || [];
  const activityLog = [...state.activityLog];
  let lastEventSeq = state.lastEventSeq;
  for (const e of events) {
    activityLog.push({
      seq: e.seq,
      at: e.at || new Date().toISOString(),
      step: e.step,
      service: e.service,
      action: e.action,
      detail: e.detail,
      preview: e.preview,
    });
    if (e.seq > lastEventSeq) lastEventSeq = e.seq;
  }
  if (events.length) {
    updateState({
      activityLog: activityLog.slice(-1000),
      lastEventSeq,
    }, 'run:activity-recorded');
  }
  return events.length;
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
    const detailText = entry.action
      ? `${entry.action}${entry.detail ? `: ${entry.detail}` : ''}`
      : (entry.detail || '');
    const row = el('div', { class: 'activity-row' },
      el('span', { class: 'activity-time',
                   text: (entry.at || '').slice(11, 19) }),
      el('span', { class: 'activity-step', text: entry.step || '' }),
      el('span', { class: 'activity-service', text: entry.service }),
      el('span', { class: 'activity-detail', text: detailText }),
    );
    if (entry.preview) {
      row.classList.add('has-preview');
      const pre = el('pre', { class: 'activity-preview', text: entry.preview });
      row.addEventListener('click', () => row.classList.toggle('is-expanded'));
      log.append(row, pre);
    } else {
      log.append(row);
    }
  }
  // Follow the tail unless the reader has scrolled up to look at history
  if (atBottom) log.scrollTop = log.scrollHeight;
}

/* A one-second timer so elapsed time moves between polls. */
function startElapsedTicker() {
  stopElapsedTicker();
  updateState({
    elapsedTimer: setInterval(() => {
      const node = $('#live-elapsed');
      if (!node) return;
      const started = node.dataset.started;
      if (!started) return;
      const ms = Date.now() - new Date(started).getTime();
      node.textContent = fmtDuration(ms);
    }, 1000),
  }, 'run-monitor:elapsed-started');
}

function stopElapsedTicker() {
  if (state.elapsedTimer) clearInterval(state.elapsedTimer);
  updateState({ elapsedTimer: null }, 'run-monitor:elapsed-stopped');
}

/*
 * The single most recent data preview for a running step, condensed to one
 * line, so the overview card shows a glimpse of what's actually moving
 * (a query, a result title, a model response) rather than only the coarse
 * "Consensus — searching" status line above it.
 */
function latestPreviewLine(stepName) {
  const stepMeta = state.status && state.status.steps.find(s => s.name === stepName);
  const label = stepMeta ? stepMeta.label : null;
  for (let i = state.activityLog.length - 1; i >= 0; i--) {
    const entry = state.activityLog[i];
    if (label && entry.step !== label) continue;
    if (entry.preview) {
      const oneLine = entry.preview.split('\n')[0];
      return el('p', { class: 'live-preview', text: oneLine.slice(0, 160) });
    }
    return null;
  }
  return null;
}

export function renderOverview(status) {
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
  updateState({ _lastDetailSig: sig }, 'run:detail-signature-updated');

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
      const srcOverrides = detail.source_overrides || {};
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
      updateState({ tab: 'overview' }, 'run:rerun-requested');
      switchTab('overview');
      await refreshStatus();
      startPolling();
    });
  }).catch(err => toast(err.message, 'error'));
}

// F11: Branch from a completed step — clone the run's state up to this
// step into a new run, preserving the original for comparison.
function confirmBranch(stepName) {
  const stepLabel = pipeline_label(stepName);
  const problemInput = el('textarea', {
    rows: 3, style: 'width:100%;',
    placeholder: 'Leave blank to keep the original problem',
  });
  const body = el('div', {},
    el('p', {},
      'Branching from ',
      el('strong', { text: stepLabel }),
      ' creates a new run with everything up to this step already done. ',
      'The original run is preserved unchanged.'),
    el('p', { class: 'muted small' },
      'The new run starts from the next step with the cloned sources, tree, ',
      'and break instructions in place. Useful for exploring a specific gap ',
      'or proposal in depth without re-running the gathering steps.'),
    el('label', { class: 'field', style: 'margin-top:.6rem;' },
      el('span', { class: 'field-label' }, 'New problem (optional)'),
      problemInput,
      el('span', { class: 'field-hint' },
        'Change the problem to explore a different angle, or leave blank to ',
        'keep the original.'),
    ),
  );
  openModal('Branch from ' + stepLabel, body, async () => {
    const resp = await api(`/api/runs/${state.runId}/branch`, {
      method: 'POST', body: {
        branch_after_step: stepName,
        new_problem: problemInput.value.trim(),
      },
    });
    toast(`Branched into ${resp.new_run_id.slice(-8)}.`, 'ok');
    await openRun(resp.new_run_id);
    startPolling();
  });
}

// Look up a step's display label from the current run's step list.
function pipeline_label(stepName) {
  const step = (state.status?.steps || []).find(s => s.name === stepName);
  return step?.label || stepName;
}

on('navigation:changed', ({ view }) => {
  if (view !== 'run') stopPolling();
});
