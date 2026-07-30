export const POLL_ACTIVE_MS = 2000;
export const POLL_IDLE_MS = 15000;

const state = Object.seal({
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
  eventSource: null,  // SSE connection (F3)
  activityLog: [],    // accumulated service notes for this run, with previews
  lastEventSeq: 0,    // cursor into step_events — only fetch what's new
  elapsedTimer: null,
  isAdmin: false,
  storage: '',
  settings: null,
  runs: [],
  runsTotal: 0,
  runsPageSize: null,
  _lastDetailSig: '',
});

const listeners = new Set();

export function getState() {
  return state;
}

export function updateState(patch, action = 'state:update') {
  const keys = Object.keys(patch);
  const previous = Object.fromEntries(keys.map(key => [key, state[key]]));
  Object.assign(state, patch);
  const change = Object.freeze({ action, keys, state, previous });
  listeners.forEach(listener => listener(change));
  return state;
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export { state };
