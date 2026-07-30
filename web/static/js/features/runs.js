import { api } from '../core/api-client.js?v=22';
import { $, clear, el, shortTime } from '../core/dom.js?v=22';
import { showView } from '../core/navigation.js?v=22';
import { state, updateState } from '../core/store.js?v=22';
import { toast } from '../components/toast.js?v=22';
import { openRun } from './run-detail.js?v=22';
import { showSettings, switchSettingsTab } from './settings.js?v=22';

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

export async function showRuns() {
  showView('runs');
  buildRunFilterBar();
  renderSetupCard();

  const data = await api(`/api/runs?limit=${runsPageSize()}&offset=0`);
  updateState({
    runs: data.runs || [],
    runsTotal: data.total,
  }, 'runs:loaded');
  renderRunList();
}

/* Status buckets, matching what runPill() already distinguishes. */
const RUN_STATUS_FILTERS = [
  { id: 'running',  label: 'Running'  },
  { id: 'break',    label: 'Awaiting break' },
  { id: 'complete', label: 'Complete' },
  { id: 'failed',   label: 'Failed'   },
  { id: 'stopped',  label: 'Stopped'  },
];

function runBucket(run) {
  if (run.awaiting_break !== null && run.awaiting_break !== undefined) return 'break';
  if ((run.status || '').startsWith('failed')) return 'failed';
  if (run.status === 'completed') return 'complete';
  if (run.status === 'cancelled' || run.status === 'cancelling') return 'stopped';
  return 'running';
}

export const runFilters = { query: '', statuses: new Set(), sort: 'recent' };
const RUNS_PAGE_SIZE = 25;

/* display.runs_per_page, once preferences have loaded. */
export function runsPageSize() { return state.runsPageSize || RUNS_PAGE_SIZE; }

/* Honours display.show_run_ids and display.timestamp_format. */
function runCardMeta(run) {
  const settings = state.settings || {};
  const showId = settings['display.show_run_ids'] !== false;
  const when = settings['display.timestamp_format'] === 'absolute'
    ? new Date(run.created_at).toLocaleString()
    : shortTime(run.created_at);
  return showId ? `${run.run_id} · ${when}` : when;
}

function buildRunFilterBar() {
  const chips = $('#run-status-chips');
  if (!chips || chips.childElementCount) return;   // build once
  for (const f of RUN_STATUS_FILTERS) {
    const btn = el('button', {
      class: 'chip', type: 'button', 'aria-pressed': 'false', text: f.label,
    });
    btn.addEventListener('click', () => {
      const on = runFilters.statuses.has(f.id);
      if (on) runFilters.statuses.delete(f.id); else runFilters.statuses.add(f.id);
      btn.classList.toggle('is-on', !on);
      btn.setAttribute('aria-pressed', String(!on));
      renderRunList();
    });
    chips.append(btn);
  }
  const search = $('#run-search');
  let debounce;
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      runFilters.query = search.value.trim().toLowerCase();
      renderRunList();
    }, 150);
  });
  $('#run-sort').addEventListener('change', (ev) => {
    runFilters.sort = ev.target.value;
    renderRunList();
  });
  $('#btn-load-more').addEventListener('click', loadMoreRuns);
}

function filteredRuns() {
  let runs = state.runs || [];
  if (runFilters.query) {
    runs = runs.filter(r =>
      (r.problem || '').toLowerCase().includes(runFilters.query) ||
      (r.run_id || '').toLowerCase().includes(runFilters.query));
  }
  if (runFilters.statuses.size) {
    runs = runs.filter(r => runFilters.statuses.has(runBucket(r)));
  }
  const sorted = [...runs];
  if (runFilters.sort === 'oldest') {
    sorted.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
  } else if (runFilters.sort === 'problem') {
    sorted.sort((a, b) => (a.problem || '').localeCompare(b.problem || ''));
  } else {
    sorted.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  }
  return sorted;
}

async function loadMoreRuns() {
  const btn = $('#btn-load-more');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const data = await api(`/api/runs?limit=${runsPageSize()}&offset=${(state.runs || []).length}`);
    updateState({
      runs: [...(state.runs || []), ...(data.runs || [])],
      runsTotal: data.total,
    }, 'runs:page-loaded');
    renderRunList();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Load more';
  }
}

/*
 * Renders whatever is currently in state.runs through the active filters.
 * Filtering is client-side over the pages fetched so far — the endpoint has no
 * search parameter, and paging in 25s means the common case is one request.
 */
function renderRunList() {
  const list = $('#runs-list');
  clear(list);

  const all = state.runs || [];
  const runs = filteredRuns();
  const filtering = Boolean(runFilters.query || runFilters.statuses.size);

  $('#runs-empty').hidden = all.length > 0;
  $('#runs-nomatch').hidden = !(all.length > 0 && runs.length === 0 && filtering);
  $('#run-filters').hidden = all.length === 0;
  $('#btn-load-more').hidden =
    !state.runsTotal || all.length >= state.runsTotal || filtering;

  for (const run of runs) {
    // Keyboard-operable card. A <button> can't wrap this block-level content,
    // so it carries the button role and its own Enter/Space handling instead.
    const open = () => openRun(run.run_id);
    list.append(el('div', {
      class: 'run-card', role: 'button', tabindex: '0',
      'aria-label': `Open run: ${run.problem}`,
      onClick: open,
      onKeyDown: (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); }
      },
    },
      el('div', {},
        el('div', { class: 'run-card-problem', text: run.problem }),
        el('div', { class: 'run-card-meta', text: runCardMeta(run) }),
      ),
      el('div', { class: 'run-card-right' },
        runPill(run),
        el('div', { class: 'run-card-meta',
                    text: `${run.progress.done}/${run.progress.total} steps` }),
      ),
    ));
  }
}

/*
 * A1: a fresh account has no provider credential, and nothing said so. You
 * could write a problem statement, tune models, press Start, and only then be
 * told to "PUT /api/credentials first". Gate the flow on the credential and
 * point at Settings instead.
 */
function renderSetupCard() {
  const card = $('#setup-card');
  if (!card) return;
  const connected = (state.providers || []).length > 0;

  card.hidden = connected;
  $('#btn-new-run').disabled = !connected;
  $('#btn-new-run').title = connected
    ? '' : 'Connect a model provider first';
  if (connected) { clear(card); return; }

  clear(card);
  const step = (done, text, action) => el('li', { class: done ? 'is-done' : '' },
    el('span', { class: 'setup-mark', 'aria-hidden': 'true', text: done ? '✓' : '○' }),
    el('span', { class: 'sr-only', text: done ? 'Done: ' : 'To do: ' }),
    el('span', { text }),
    action || null,
  );

  const connectBtn = el('button', { class: 'btn btn-primary btn-small', type: 'button' },
    'Connect a provider');
  connectBtn.addEventListener('click', () => {
    showSettings();
    switchSettingsTab('providers');
  });
  const keysBtn = el('button', { class: 'btn btn-small', type: 'button' }, 'Add keys');
  keysBtn.addEventListener('click', () => {
    showSettings();
    switchSettingsTab('sources');
  });

  card.append(
    el('h2', { text: 'Finish setting up SEEKER' }),
    el('p', { class: 'muted small', text:
      'SEEKER runs on your own model provider — nothing is shared between accounts.' }),
    el('ol', { class: 'setup-steps' },
      step(true,  'Account created'),
      step(false, 'Connect a model provider — required', connectBtn),
      step(false, 'Add source API keys — optional, most sources work without one', keysBtn),
    ),
  );
}
