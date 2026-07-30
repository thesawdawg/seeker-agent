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

const POLL_ACTIVE_MS = 2000;   // work is moving — SSE fallback
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
  eventSource: null,  // SSE connection (F3)
  activityLog: [],    // accumulated service notes for this run, with previews
  lastEventSeq: 0,    // cursor into step_events — only fetch what's new
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

/* ── Markdown renderer ──────────────────────────────────────────────── */
/* Lightweight, dependency-free markdown-to-HTML for artifact display.  */
/* Handles: headings, bold, italic, inline code, code blocks, unordered  */
/* and ordered lists, blockquotes, horizontal rules, and paragraphs.    */
function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderMarkdown(md) {
  if (!md) return '';
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;
  let inCode = false;
  let codeLang = '';
  let codeBuf = [];
  let listType = null; // 'ul' or 'ol'
  let listBuf = [];
  let paraBuf = [];

  function inline(text) {
    let s = escapeHtml(text);
    // inline code
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    // bold
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    // italic
    s = s.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
    s = s.replace(/(?<!_)_([^_]+)_(?!_)/g, '<em>$1</em>');
    // links [text](url) — url must be http/https/mailto
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return s;
  }

  function flushPara() {
    if (paraBuf.length) {
      out.push('<p>' + paraBuf.map(inline).join('<br>') + '</p>');
      paraBuf = [];
    }
  }
  function flushList() {
    if (listBuf.length) {
      out.push(`<${listType}>` + listBuf.join('') + `</${listType}>`);
      listBuf = [];
      listType = null;
    }
  }
  function flushAll() { flushPara(); flushList(); }

  while (i < lines.length) {
    const line = lines[i];

    // Code block fence
    if (line.match(/^```/)) {
      if (inCode) {
        out.push('<pre><code>' + escapeHtml(codeBuf.join('\n')) + '</code></pre>');
        codeBuf = [];
        inCode = false;
        codeLang = '';
      } else {
        flushAll();
        inCode = true;
        codeLang = line.replace(/^```/, '').trim();
      }
      i++;
      continue;
    }
    if (inCode) { codeBuf.push(line); i++; continue; }

    // Horizontal rule
    if (line.match(/^---+\s*$/) || line.match(/^\*\*\*+\s*$/)) {
      flushAll();
      out.push('<hr>');
      i++;
      continue;
    }

    // Headings
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushAll();
      const level = h[1].length;
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    // Blockquote
    if (line.match(/^>\s?/)) {
      flushAll();
      const quoteLines = [];
      while (i < lines.length && lines[i].match(/^>\s?/)) {
        quoteLines.push(lines[i].replace(/^>\s?/, ''));
        i++;
      }
      out.push('<blockquote>' + quoteLines.map(inline).join('<br>') + '</blockquote>');
      continue;
    }

    // Unordered list
    if (line.match(/^[-*+]\s+/)) {
      flushPara();
      if (listType !== 'ul') { flushList(); listType = 'ul'; }
      const itemText = line.replace(/^[-*+]\s+/, '');
      // Handle nested indentation
      const nested = itemText.match(/^(\s+)(.*)$/);
      if (nested && listBuf.length) {
        listBuf[listBuf.length - 1] += '<br>' + inline(nested[2]);
      } else {
        listBuf.push('<li>' + inline(itemText) + '</li>');
      }
      i++;
      continue;
    }

    // Ordered list
    if (line.match(/^\d+\.\s+/)) {
      flushPara();
      if (listType !== 'ol') { flushList(); listType = 'ol'; }
      const itemText = line.replace(/^\d+\.\s+/, '');
      listBuf.push('<li>' + inline(itemText) + '</li>');
      i++;
      continue;
    }

    // Non-list line ends a list
    if (listType) flushList();

    // Blank line ends a paragraph
    if (line.trim() === '') {
      flushPara();
      i++;
      continue;
    }

    // Accumulate paragraph
    paraBuf.push(line);
    i++;
  }

  // Flush remaining
  if (inCode) out.push('<pre><code>' + escapeHtml(codeBuf.join('\n')) + '</code></pre>');
  flushAll();

  return out.join('\n');
}

function mdToElement(md) {
  const div = document.createElement('div');
  div.className = 'markdown-body';
  div.innerHTML = renderMarkdown(md);
  return div;
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
  // --- Auth state: which method tab is active, and whether we're registering ---
  let loginMethod = 'apikey';   // 'apikey' | 'password'
  let isRegistering = false;

  const providerSelect = $('#login-provider');
  if (providerSelect) {
    providerSelect.addEventListener('change', ev => {
      const preset = BASE_URL_DEFAULTS[ev.target.value];
      if (preset) $('#login-base-url').value = preset;
      $('#login-base-hint').innerHTML = ev.target.value === 'open-webui'
        ? 'Open-WebUI: include the <code>/api</code> prefix.'
        : 'The endpoint root, without <code>/chat/completions</code>.';
    });
  }

  // --- Tab switching (API key ↔ Username) ---
  // Toggling `disabled` on inputs in hidden panels is critical: the API key
  // panel has `required` fields, and the browser blocks form submission if
  // those are still active when the panel is hidden. Disabled inputs are
  // excluded from native form validation.
  function setPanelEnabled(panel, enabled) {
    if (!panel) return;
    panel.querySelectorAll('input, select, textarea').forEach(el => {
      el.disabled = !enabled;
    });
  }

  function switchMethod(method) {
    loginMethod = method;
    try { localStorage.setItem('seeker-auth-method', method); } catch { /* private mode */ }
    document.querySelectorAll('.auth-tab').forEach(t =>
      t.classList.toggle('active', t.dataset.method === method));
    const apikeyPanel = $('#login-apikey');
    const passwordPanel = $('#login-password');
    const showApikey = (method === 'apikey');
    apikeyPanel.hidden = !showApikey;
    passwordPanel.hidden = showApikey;
    setPanelEnabled(apikeyPanel, showApikey);
    setPanelEnabled(passwordPanel, !showApikey);
    // Reset register mode when switching tabs
    if (method !== 'password') setRegisterMode(false);
    // Update submit button label
    $('#btn-login').textContent = 'Sign in';
    $('#login-error').hidden = true;
  }

  document.querySelectorAll('.auth-tab').forEach(tab => {
    tab.addEventListener('click', () => switchMethod(tab.dataset.method));
  });

  // --- Register / login toggle (password method only) ---
  function setRegisterMode(on) {
    isRegistering = on;
    const regFields = $('#register-fields');
    regFields.hidden = !on;
    setPanelEnabled(regFields, on);
    $('#password-panel-hint').textContent = on
      ? 'Create a new account with a username and password.'
      : 'Sign in with your username and password.';
    $('#btn-login').textContent = on ? 'Register' : 'Sign in';
    $('#btn-toggle-register').textContent = on
      ? 'Already have an account? Sign in'
      : 'Need an account? Register';
    $('#login-error').hidden = true;
  }

  const toggleBtn = $('#btn-toggle-register');
  if (toggleBtn) {
    toggleBtn.addEventListener('click', () =>
      setRegisterMode(!isRegistering));
  }

  // --- Form submit — dispatches to the right endpoint ---
  $('#form-login').addEventListener('submit', async ev => {
    ev.preventDefault();
    const button = $('#btn-login');
    const error = $('#login-error');
    error.hidden = true;
    button.disabled = true;

    try {
      if (loginMethod === 'apikey') {
        button.textContent = 'Checking your key…';
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
      } else {
        // Password method
        const username = $('#login-username').value.trim();
        const password = $('#login-pw').value;
        if (!username || !password) {
          throw new Error('Username and password are required');
        }
        if (isRegistering) {
          const pw2 = $('#login-pw2').value;
          if (password !== pw2) {
            throw new Error('Passwords do not match');
          }
          if (password.length < 8) {
            throw new Error('Password must be at least 8 characters');
          }
          button.textContent = 'Creating account…';
          await api('/api/auth/password/register', {
            method: 'POST',
            body: {
              username,
              password,
              display_name: $('#login-pw-name').value.trim(),
            },
          });
          $('#login-pw').value = '';
          $('#login-pw2').value = '';
        } else {
          button.textContent = 'Signing in…';
          await api('/api/auth/password/login', {
            method: 'POST',
            body: { username, password },
          });
          $('#login-pw').value = '';
        }
      }
      await afterSignIn();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = isRegistering ? 'Register' : 'Sign in';
    }
  });

  // SSO button — redirect to the SAML backend's login endpoint.
  // The SSO section is only visible when the SAML backend is enabled
  // (discovered via /api/auth/methods on page load).
  const ssoBtn = $('#btn-sso');
  if (ssoBtn) {
    ssoBtn.addEventListener('click', () => {
      // Redirect to the SP-initiated SAML login; the IdP will redirect
      // back to /api/auth/saml/callback which sets the session cookie
      // and redirects to the app root.
      window.location.href = '/api/auth/saml/login';
    });
  }

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

  // Set initial panel disabled state (password panel starts hidden)
  switchMethod('apikey');
}

/**
 * Discover available auth methods and show/hide UI accordingly.
 * Called on page load before the user signs in.
 */
async function discoverAuthMethods() {
  try {
    const body = await api('/api/auth/methods');
    const methods = body.methods || [];

    // Show SSO button if SAML is enabled
    const saml = methods.find(m => m.name === 'saml' && m.enabled);
    if (saml) {
      const section = $('#sso-section');
      if (section) section.hidden = false;
    }

    // Hide the Username tab if the password backend is not available
    const password = methods.find(m => m.name === 'password' && m.enabled);
    if (!password) {
      const tab = document.querySelector('.auth-tab[data-method="password"]');
      if (tab) tab.hidden = true;
    }

    // Reopen on whichever method signed you in last. The form defaulted to
    // "API key" every visit, so password users clicked the same tab each time.
    // Preference only — never the credential, and never before checking the
    // backend is still enabled.
    const last = localStorage.getItem('seeker-auth-method');
    if (last && last !== 'apikey') {
      const stillEnabled = methods.find(m => m.name === last && m.enabled);
      const tab = document.querySelector(`.auth-tab[data-method="${last}"]`);
      if (stillEnabled && tab && !tab.hidden) tab.click();
    }
  } catch {
    // If the endpoint isn't available (older server), defaults are fine.
  }
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

  // F12: check admin status to show/hide the Admin button
  try {
    const who = await api('/api/config/whoami');
    state.isAdmin = who.is_admin;
    $('#btn-admin').hidden = !who.is_admin;
  } catch {
    state.isAdmin = false;
    $('#btn-admin').hidden = true;
  }

  // Storage backend badge: admins only (see init()).
  const badge = $('#storage-badge');
  if (badge) {
    badge.textContent = state.isAdmin ? (state.storage || '') : '';
    badge.hidden = !state.isAdmin;
  }

  // Account-scoped preferences. Advisory — a failure here must not block
  // sign-in, so the app falls back to the built-in defaults.
  try {
    const prefs = await api('/api/settings');
    state.settings = prefs.settings;
    applyPreferences(prefs.settings);
  } catch { /* defaults are fine */ }

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
  buildRunFilterBar();
  renderSetupCard();

  const data = await api(`/api/runs?limit=${runsPageSize()}&offset=0`);
  state.runs = data.runs || [];
  state.runsTotal = data.total;
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

const runFilters = { query: '', statuses: new Set(), sort: 'recent' };
const RUNS_PAGE_SIZE = 25;

/* display.runs_per_page, once preferences have loaded. */
function runsPageSize() { return state.runsPageSize || RUNS_PAGE_SIZE; }

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
    state.runs = [...(state.runs || []), ...(data.runs || [])];
    state.runsTotal = data.total;
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
  state.lastEventSeq = 0;
  state._lastDetailSig = null;  // force a fresh detail fetch for the new run
  showView('run');

  const detail = await api(`/api/runs/${runId}`);
  $('#run-problem').textContent = detail.problem;
  $('#run-meta').textContent = `${runId} · started ${shortTime(detail.created_at)}`;

  await refreshStatus();
  startPolling();
}

// F3: SSE event stream — replaces the 2s polling loop. Falls back to
// polling if EventSource is unavailable or the connection fails.
function startPolling() {
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
  state.eventSource = es;

  es.addEventListener('status', async (ev) => {
    try {
      const status = JSON.parse(ev.data);
      const previous = state.status;
      state.status = status;
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
    state.eventSource = null;
  });

  es.addEventListener('error', (ev) => {
    // EventSource auto-reconnects, but if the connection keeps failing
    // (e.g. behind a proxy that doesn't support SSE), fall back to
    // polling after the first error.
    if (es.readyState === EventSource.CLOSED) {
      state.eventSource = null;
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
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  stopElapsedTicker();
}

async function refreshStatus() {
  const previous = state.status;
  const status = await api(`/api/runs/${state.runId}/status?since_seq=${state.lastEventSeq}`);
  state.status = status;

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
  for (const e of events) {
    state.activityLog.push({
      seq: e.seq,
      at: e.at || new Date().toISOString(),
      step: e.step,
      service: e.service,
      action: e.action,
      detail: e.detail,
      preview: e.preview,
    });
    if (e.seq > state.lastEventSeq) state.lastEventSeq = e.seq;
  }
  if (events.length) {
    // Keep memory bounded on very long runs
    if (state.activityLog.length > 1000) {
      state.activityLog = state.activityLog.slice(-1000);
    }
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
      state.tab = 'overview';
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
        'Each work includes full references and backlinks (DOI/URL) so you can read the original and validate the claims. Disagree with why something was called seminal? Note it in the override box.'),
    );
    for (const source of seminal.slice(0, 30)) {
      const id = source.source_id;
      const item = el('div', { class: 'item item-referenced' });

      // Title line
      item.append(el('div', { class: 'item-title',
        text: `${source.year || 'n.d.'} — ${source.title || ''}` }));

      // Seminal reason
      item.append(el('div', { class: 'item-meta',
        text: source.seminal_reason || '' }));

      // Full reference with backlinks
      const ref = el('div', { class: 'ref-block' });

      // Authors
      let authors = source.authors;
      if (typeof authors === 'string') {
        try { authors = JSON.parse(authors); } catch { /* keep as string */ }
      }
      if (Array.isArray(authors) && authors.length) {
        const names = authors.map(a => typeof a === 'string' ? a : (a.name || a.full_name || ''));
        ref.append(el('div', { class: 'ref-authors', text: names.join(', ') }));
      }

      // Source/venue
      if (source.source_name) {
        ref.append(el('div', { class: 'ref-venue muted small',
          text: source.source_name }));
      }

      // DOI and URL backlinks
      const links = el('div', { class: 'ref-links' });
      const doi = (source.doi || '').trim();
      if (doi) {
        const doiUrl = doi.startsWith('http') ? doi : `https://doi.org/${doi}`;
        links.append(el('a', {
          href: doiUrl, target: '_blank', rel: 'noopener noreferrer',
          class: 'ref-link', text: `DOI: ${doi}`,
        }));
      }
      const activeLink = (source.active_link || '').trim();
      const doiUrlForCompare = doi ? (doi.startsWith('http') ? doi : `https://doi.org/${doi}`) : '';
      if (activeLink && activeLink !== doiUrlForCompare) {
        links.append(el('a', {
          href: activeLink, target: '_blank', rel: 'noopener noreferrer',
          class: 'ref-link', text: 'View source →',
        }));
      }
      // Library catalog link (from Librarian step)
      const catalogUrl = (source.catalog_url || '').trim();
      if (catalogUrl && catalogUrl !== activeLink && catalogUrl !== doiUrlForCompare) {
        links.append(el('a', {
          href: catalogUrl, target: '_blank', rel: 'noopener noreferrer',
          class: 'ref-link', text: 'Library catalog →',
        }));
      }
      if (links.children.length) ref.append(links);

      // URL validation warnings (from server-side pre-check)
      const urlIssues = source._url_issues || [];
      if (Array.isArray(urlIssues) && urlIssues.length) {
        const warn = el('div', { class: 'ref-url-warnings' });
        for (const issue of urlIssues) {
          warn.append(el('span', { class: 'url-warning-badge', text: `⚠ ${issue}` }));
        }
        ref.append(warn);
      }

      // Library catalog availability badge (from Librarian step)
      let availRaw = source.availability;
      if (typeof availRaw === 'string' && availRaw) {
        try { availRaw = JSON.parse(availRaw); } catch { /* keep as string */ }
      }
      if (Array.isArray(availRaw) && availRaw.length) {
        const availEl = el('div', { class: 'ref-availability' });
        for (const a of availRaw) {
          availEl.append(el('span', { class: 'avail-badge', text: String(a) }));
        }
        ref.append(availEl);
      }

      // Abstract excerpt
      const abstract = (source.abstract || '').trim();
      if (abstract) {
        const excerpt = abstract.length > 300
          ? abstract.slice(0, 300) + '…'
          : abstract;
        ref.append(el('div', { class: 'ref-abstract muted small',
          text: excerpt }));
      }

      item.append(ref);

      // Override textarea
      item.append(el('textarea', {
        rows: 1, placeholder: 'Override this assessment…',
        onInput: ev => {
          const value = ev.target.value.trim();
          if (value) draft.seminalOverrides.set(id, value);
          else draft.seminalOverrides.delete(id);
          updatePreview();
        },
      }));

      group.append(item);
    }
    panel.append(group);
  }

  if (historical.length) {
    const group = el('div', { class: 'review-group' },
      el('h3', {}, 'Historical map',
        el('span', { class: 'muted small', text: `${historical.length} entries` })),
    );
    for (const source of historical.slice(0, 25)) {
      const item = el('div', { class: 'item item-referenced' });
      item.append(el('div', { class: 'item-title',
        text: `${source.year || 'n.d.'} — ${source.title || ''} [${source.phase_tag || ''}]` }));
      item.append(el('div', { class: 'item-meta',
        text: source.historical_reason || '' }));

      // Reference backlinks for historical works too
      const ref = el('div', { class: 'ref-block' });
      const doi = (source.doi || '').trim();
      const activeLink = (source.active_link || '').trim();
      const links = el('div', { class: 'ref-links' });
      if (doi) {
        const doiUrl = doi.startsWith('http') ? doi : `https://doi.org/${doi}`;
        links.append(el('a', {
          href: doiUrl, target: '_blank', rel: 'noopener noreferrer',
          class: 'ref-link', text: `DOI: ${doi}`,
        }));
      }
      if (activeLink && activeLink !== (doi ? (doi.startsWith('http') ? doi : `https://doi.org/${doi}`) : '')) {
        links.append(el('a', {
          href: activeLink, target: '_blank', rel: 'noopener noreferrer',
          class: 'ref-link', text: 'View source →',
        }));
      }
      if (links.children.length) ref.append(links);

      // URL validation warnings (from server-side pre-check)
      const urlIssues = source._url_issues || [];
      if (Array.isArray(urlIssues) && urlIssues.length) {
        const warn = el('div', { class: 'ref-url-warnings' });
        for (const issue of urlIssues) {
          warn.append(el('span', { class: 'url-warning-badge', text: `⚠ ${issue}` }));
        }
        ref.append(warn);
      }

      const abstract = (source.abstract || '').trim();
      if (abstract) {
        const excerpt = abstract.length > 300
          ? abstract.slice(0, 300) + '…'
          : abstract;
        ref.append(el('div', { class: 'ref-abstract muted small',
          text: excerpt }));
      }
      if (ref.children.length) item.append(ref);

      group.append(item);
    }
    panel.append(group);
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
    // Surface the combined report at the top — it is the headline output.
    const sorted = [...artifacts].sort((a, b) => {
      if (a.output_type === 'combined_report') return -1;
      if (b.output_type === 'combined_report') return 1;
      return 0;
    });
    const combined = sorted.find(a => a.output_type === 'combined_report');
    if (combined) {
      panel.append(el('div', { class: 'combined-report-callout' },
        el('h3', {}, 'Combined Report'),
        el('p', { class: 'muted small' },
          'A single self-contained HTML file bundling every artifact below ' +
          'with charts, a cover page, and a table of contents. Opens in any ' +
          'browser and can be saved to PDF via Print.'),
        el('div', { class: 'item-actions' },
          el('button', { class: 'btn btn-primary btn-small', type: 'button',
            onClick: async ev => {
              ev.target.disabled = true;
              try {
                const full = await api(
                  `/api/runs/${state.runId}/artifacts/${combined.artifact_id}`);
                if (!full.content) { toast('Report file is missing', 'error'); return; }
                const blob = new Blob([full.content], { type: 'text/html' });
                const url = URL.createObjectURL(blob);
                window.open(url, '_blank');
                // Revoke after a delay so the tab can load
                setTimeout(() => URL.revokeObjectURL(url), 60000);
              } catch (err) {
                toast(err.message, 'error');
              } finally { ev.target.disabled = false; }
            },
          }, 'Open report'),
          el('button', { class: 'btn btn-small', type: 'button',
            onClick: async ev => {
              ev.target.disabled = true;
              try {
                const full = await api(
                  `/api/runs/${state.runId}/artifacts/${combined.artifact_id}`);
                if (!full.content) { toast('Report file is missing', 'error'); return; }
                const blob = new Blob([full.content], { type: 'text/html' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${state.runId}_combined_report.html`;
                document.body.append(a);
                a.click();
                a.remove();
                setTimeout(() => URL.revokeObjectURL(url), 10000);
              } catch (err) {
                toast(err.message, 'error');
              } finally { ev.target.disabled = false; }
            },
          }, 'Download'),
        ),
      ));
      panel.append(el('hr', {}));
    }
    panel.append(el('h3', {}, 'Scribe artifacts'),
      el('p', { class: 'muted small' },
        'Final outputs produced by Scribe at the end of the run.'));
    for (const artifact of sorted) {
      if (artifact.output_type === 'combined_report') continue;  // shown above
      const body = el('div', { hidden: true });
      const isHtml = artifact.format === 'html';
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
                if (isHtml && full.content) {
                  // Render HTML artifacts in a sandboxed iframe via blob URL
                  const blob = new Blob([full.content], { type: 'text/html' });
                  const url = URL.createObjectURL(blob);
                  const frame = el('iframe', {
                    src: url,
                    sandbox: 'allow-same-origin',
                    class: 'html-artifact-frame',
                  });
                  body.append(frame);
                  body.append(el('p', { class: 'muted small' },
                    'Rendered in a sandboxed iframe. Right-click → Reload if it appears blank.'));
                } else {
                  body.append(mdToElement(full.content || '(the file is missing on disk)'));
                }
                body.hidden = false;
                ev.target.textContent = 'Hide';
              } catch (err) {
                toast(err.message, 'error');
              } finally { ev.target.disabled = false; }
            },
          }, 'View'),
          isHtml && el('button', { class: 'btn btn-small', type: 'button',
            onClick: async ev => {
              ev.target.disabled = true;
              try {
                const full = await api(
                  `/api/runs/${state.runId}/artifacts/${artifact.artifact_id}`);
                if (!full.content) { toast('File is missing', 'error'); return; }
                const blob = new Blob([full.content], { type: 'text/html' });
                const url = URL.createObjectURL(blob);
                window.open(url, '_blank');
                setTimeout(() => URL.revokeObjectURL(url), 60000);
              } catch (err) {
                toast(err.message, 'error');
              } finally { ev.target.disabled = false; }
            },
          }, 'Open in tab'),
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
                body.append(mdToElement(full.content || '(empty)'));
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

  panel.append(el('div', { class: 'review-group' },
    el('h3', {}, 'Argument Tree'),
    el('p', { class: 'muted small' },
      'Every claim traces to evidence. Click a node to inspect its source and metadata. ' +
      'Search or filter by type below; matches are highlighted and their ' +
      'ancestors kept for context. Content wraps fully — no truncation.'),
  ));

  /*
   * Filter bar.
   *
   * This replaces two separate rows that both looked interactive and were not:
   * a stats row ("8 Question", "28 ⚠ weak") and a colour legend ("Root",
   * "Question", …). Users read them as filter chips and clicked to no effect.
   * They are now one row of real toggles that carry the same counts and the
   * same colour coding, so the legend is implicit in the control.
   */
  const treeFilter = { query: '', types: new Set(), statuses: new Set() };
  const byType = stats.by_type || {};
  const claimStatuses = stats.claim_statuses || {};

  const search = el('input', {
    type: 'search', id: 'tree-search', class: 'tree-search',
    placeholder: 'Search node text…', autocomplete: 'off',
  });
  let searchDebounce;
  search.addEventListener('input', () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      treeFilter.query = search.value.trim().toLowerCase();
      applyTreeFilter();
    }, 150);
  });

  const chipRow = el('div', { class: 'tree-filter-chips', role: 'group',
                              'aria-label': 'Filter tree nodes' });

  const makeChip = (label, count, extraClass, set, key) => {
    const chip = el('button', {
      class: `tree-chip ${extraClass}`, type: 'button', 'aria-pressed': 'false',
    },
      el('span', { class: 'tree-chip-count', text: String(count) }),
      el('span', { text: label }),
    );
    chip.addEventListener('click', () => {
      const on = set.has(key);
      if (on) set.delete(key); else set.add(key);
      chip.classList.toggle('is-on', !on);
      chip.setAttribute('aria-pressed', String(!on));
      applyTreeFilter();
    });
    chipRow.append(chip);
    return chip;
  };

  for (const t of ['question', 'claim', 'evidence', 'counter', 'bridge',
                   'historical', 'external', 'audit_note']) {
    if (byType[t]) {
      makeChip(TREE_TYPE_LABEL[t] || t, byType[t],
               TREE_NODE_COLORS[t] || '', treeFilter.types, t);
    }
  }
  for (const [s, n] of Object.entries(claimStatuses)) {
    if (TREE_STATUS_BADGE[s]) {
      makeChip(TREE_STATUS_BADGE[s], n, 'tree-chip-status', treeFilter.statuses, s);
    }
  }

  const matchCount = el('span', { class: 'tree-match-count', 'aria-live': 'polite' });
  const clearBtn = el('button', { class: 'btn btn-small', type: 'button',
                                  text: 'Clear filters', hidden: true });
  clearBtn.addEventListener('click', () => {
    treeFilter.query = '';
    treeFilter.types.clear();
    treeFilter.statuses.clear();
    search.value = '';
    chipRow.querySelectorAll('.tree-chip').forEach(c => {
      c.classList.remove('is-on');
      c.setAttribute('aria-pressed', 'false');
    });
    applyTreeFilter();
  });

  panel.append(el('div', { class: 'tree-filter-bar' },
    search, chipRow,
    el('div', { class: 'tree-filter-meta' },
      stats.unique_sources
        ? el('span', { class: 'muted small', text: `${stats.unique_sources} sources` })
        : null,
      matchCount, clearBtn),
  ));

  // Toolbar: expand/collapse all + breadcrumb container
  const breadcrumbEl = el('div', { class: 'tree-breadcrumb', id: 'tree-breadcrumb' });
  const toolbar = el('div', { class: 'tree-toolbar' },
    el('button', { class: 'btn', type: 'button', text: '▸ Expand all',
      onClick: () => {
        panel.querySelectorAll('.tree-children').forEach(c => c.hidden = false);
        panel.querySelectorAll('.tree-toggle:not(.tree-toggle-leaf)').forEach(t => t.textContent = '▾');
      },
    }),
    el('button', { class: 'btn', type: 'button', text: '▾ Collapse all',
      onClick: () => {
        // Collapse all .tree-children except the root's immediate children
        panel.querySelectorAll('.tree-children').forEach(c => {
          const wrapper = c.parentElement;  // .tree-node-wrapper
          const grandparent = wrapper && wrapper.parentElement;
          // Root wrapper is a direct child of the panel — keep its children open
          const isRootLevel = grandparent === panel;
          c.hidden = !isRootLevel;
        });
        panel.querySelectorAll('.tree-toggle:not(.tree-toggle-leaf)').forEach(t => {
          const wrapper = t.closest('.tree-node-wrapper');
          const childContainer = wrapper && wrapper.querySelector(':scope > .tree-children');
          if (childContainer) t.textContent = childContainer.hidden ? '▸' : '▾';
        });
      },
    }),
  );
  panel.append(toolbar);
  panel.append(breadcrumbEl);

  // Detail panel (shown when a node is clicked)
  const detail = el('div', { class: 'tree-detail', id: 'tree-detail' });
  panel.append(detail);

  // Build a parent map for breadcrumb navigation
  const parentMap = new Map();
  const buildParentMap = (node, parent = null) => {
    if (!node) return;
    if (parent) parentMap.set(node.node_id, parent);
    for (const child of (node.children || [])) buildParentMap(child, node);
  };
  buildParentMap(tree);

  // Track currently selected node for visual highlight
  let selectedNodeEl = null;

  // node_id → rendered pieces, so the filter can show/hide without re-rendering.
  const rendered = new Map();

  // Recursive tree renderer
  const renderNode = (node, depth = 0) => {
    if (!node) return null;
    const type = node.node_type || 'unknown';
    const colorClass = TREE_NODE_COLORS[type] || '';
    const hasChildren = node.children && node.children.length > 0;

    const childContainer = el('div', { class: 'tree-children', hidden: depth > 0 });

    const toggle = hasChildren
      ? el('span', {
          class: 'tree-toggle',
          onClick: ev => {
            ev.stopPropagation();
            childContainer.hidden = !childContainer.hidden;
            toggle.textContent = childContainer.hidden ? '▸' : '▾';
          },
        }, depth > 0 ? '▸' : '▾')
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

    const childrenCount = hasChildren
      ? el('span', { class: 'tree-node-children-count',
                     text: `${node.children.length} child${node.children.length > 1 ? 'ren' : ''}` })
      : null;

    // Header line: toggle + type + badges + child count
    const header = el('div', { class: 'tree-node-header' },
      toggle, typeLabel, statusBadge, confidenceBadge, childrenCount);

    // Content line — full text, line-clamped via CSS (3 lines), click to expand
    const contentText = node.content || '';
    const contentEl = el('div', { class: 'tree-node-content', text: contentText });
    if (contentText.length > 200) {
      contentEl.title = 'Click to expand/collapse full text';
      contentEl.style.cursor = 'pointer';
      contentEl.addEventListener('click', ev => {
        ev.stopPropagation();
        contentEl.classList.toggle('is-expanded');
      });
    }

    const nodeEl = el('div', {
      class: `tree-node ${colorClass}`,
      'data-node-id': node.node_id || '',
      onClick: () => {
        // Highlight selected node
        if (selectedNodeEl) selectedNodeEl.classList.remove('is-selected');
        nodeEl.classList.add('is-selected');
        selectedNodeEl = nodeEl;
        showNodeDetail(node, sources, detail, tree, parentMap, breadcrumbEl);
      },
    },
      header,
      contentEl,
    );

    const wrapper = el('div', { class: 'tree-node-wrapper' }, nodeEl, childContainer);
    rendered.set(node.node_id, { node, wrapper, nodeEl, childContainer, toggle });

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

  const emptyMsg = el('p', { class: 'empty', hidden: true,
    text: 'No nodes match those filters.' });
  panel.append(emptyMsg);

  /*
   * Show a node when it matches, or when a descendant does — a bare match list
   * would strip the tree of the structure that gives each claim its meaning.
   * Matches are marked; ancestors kept only for context are dimmed, and the
   * path to every match is expanded so hits aren't hidden behind a collapsed
   * parent.
   *
   * Returns whether `node`'s subtree contains a match.
   */
  function applyTreeFilter() {
    const active = Boolean(treeFilter.query || treeFilter.types.size || treeFilter.statuses.size);
    clearBtn.hidden = !active;
    let matches = 0;

    const matchesSelf = (node) => {
      const type = node.node_type || 'unknown';
      if (treeFilter.types.size && !treeFilter.types.has(type)) return false;
      if (treeFilter.statuses.size && !treeFilter.statuses.has(node.status)) return false;
      if (treeFilter.query &&
          !(node.content || '').toLowerCase().includes(treeFilter.query)) return false;
      return true;
    };

    const walk = (node) => {
      const entry = rendered.get(node.node_id);
      const self = active ? matchesSelf(node) : false;
      let descendant = false;
      for (const child of (node.children || [])) {
        if (walk(child)) descendant = true;
      }
      if (!entry) return self || descendant;

      if (!active) {
        entry.wrapper.hidden = false;
        entry.nodeEl.classList.remove('is-match', 'is-context');
        return false;
      }

      const visible = self || descendant;
      entry.wrapper.hidden = !visible;
      entry.nodeEl.classList.toggle('is-match', self);
      entry.nodeEl.classList.toggle('is-context', !self && descendant);
      if (self) matches++;

      // Open the path down to any match.
      if (descendant && entry.childContainer) {
        entry.childContainer.hidden = false;
        if (entry.toggle && !entry.toggle.classList.contains('tree-toggle-leaf')) {
          entry.toggle.textContent = '▾';
        }
      }
      return visible;
    };

    walk(tree);

    matchCount.textContent = active
      ? `${matches} of ${rendered.size} nodes match` : '';
    emptyMsg.hidden = !(active && matches === 0);
  }

  applyTreeFilter();
}

function showNodeDetail(node, sources, container, root, parentMap, breadcrumbEl) {
  clear(container);
  const type = node.node_type || 'unknown';
  const meta = node.metadata || {};

  // Build breadcrumb path: root → ... → this node
  if (breadcrumbEl) {
    clear(breadcrumbEl);
    const path = [];
    let cur = node;
    while (cur) {
      path.unshift(cur);
      cur = parentMap.get(cur.node_id) || null;
    }
    for (let i = 0; i < path.length; i++) {
      const n = path[i];
      const nType = n.node_type || 'unknown';
      const label = (n.content || '').slice(0, 60) + ((n.content || '').length > 60 ? '…' : '');
      breadcrumbEl.append(el('span', {
        class: 'tree-breadcrumb-item',
        text: `${TREE_TYPE_LABEL[nType] || nType}: ${label || '(no content)'}`,
        title: n.content || '',
        onClick: () => {
          // Scroll to and highlight the clicked breadcrumb target
          const target = document.querySelector(`[data-node-id="${n.node_id}"]`);
          if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.click();
          }
        },
      }));
      if (i < path.length - 1) {
        breadcrumbEl.append(el('span', { class: 'tree-breadcrumb-sep', text: '›' }));
      }
    }
  }

  const rows = [
    ['Type', TREE_TYPE_LABEL[type] || type],
    ['Node ID', node.node_id || '—'],
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

  // Full content — always shown, not just when > 200 chars
  if (node.content) {
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

/* Keep aria-selected in step with the visual is-active class. A screen
 * reader announces the tab state from aria-selected, not from a CSS class,
 * so without this the tab strip reads as five identical buttons. */
function markSelectedTab(tabs, isSelected) {
  tabs.forEach(t => {
    const on = isSelected(t);
    t.classList.toggle('is-active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
    // Roving tabindex: one stop for the strip, arrows move within it.
    t.tabIndex = on ? 0 : -1;
  });
}

function switchTab(name) {
  state.tab = name;
  markSelectedTab($$('#run-tabs .tab'), t => t.dataset.tab === name);
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

/* ── admin / config editor (F12) ─────────────────────────────────────── */

let _adminConfig = null;  // cached config from GET /api/config

// Save one config section (F12). Used by all the admin tab save buttons.
async function saveConfigSection(section, value) {
  await api(`/api/config/sections/${section}`, {
    method: 'PUT', body: { value },
  });
  toast(`${section} saved`, 'ok');
}

async function showAdmin() {
  if (!state.isAdmin) { toast('Admin access required', 'error'); return; }
  showView('admin');
  _adminConfig = await api('/api/config');
  switchAdminTab('sources');
}

function switchAdminTab(tab) {
  markSelectedTab($$('#admin-tabs .tab'), t => t.dataset.adminTab === tab);
  $$('#view-admin .tab-panel').forEach(p => { p.hidden = true; });
  const panel = $(`#panel-admin-${tab}`);
  if (panel) { panel.hidden = false; renderAdminTab(tab, panel); }
}

async function renderAdminTab(tab, panel) {
  clear(panel);
  if (tab === 'sources')         renderAdminSources(panel);
  else if (tab === 'agent_sources') renderAdminAgentSources(panel);
  else if (tab === 'themes')     renderAdminThemes(panel);
  else if (tab === 'run_templates') renderAdminTemplates(panel);
  else if (tab === 'users')      renderAdminUsers(panel);
}

// ── Sources tab: toggle enabled/disabled, edit api_url ──
function renderAdminSources(panel) {
  const sources = _adminConfig.sources || {};
  const saveBtn = el('button', { class: 'btn btn-primary', type: 'button',
    onClick: async () => {
      saveBtn.disabled = true;
      try {
        await saveConfigSection('sources', sources);
        toast('Sources saved', 'ok');
      } catch (e) { toast(e.message, 'error'); }
      finally { saveBtn.disabled = false; }
    },
  }, 'Save sources');

  panel.append(el('p', { class: 'muted small' },
    'Toggle which academic sources are available. Disabled sources are ' +
    'hidden from the New Run screen and from agent routing.'), saveBtn);

  const grid = el('div', { class: 'admin-source-grid' });
  for (const [id, spec] of Object.entries(sources).sort()) {
    if (!spec || typeof spec !== 'object') continue;
    const cb = el('input', { type: 'checkbox', checked: !!spec.enabled });
    cb.addEventListener('change', () => { spec.enabled = cb.checked; });
    const urlInput = el('input', { type: 'text', value: spec.api_url || '',
      placeholder: 'https://...' });
    urlInput.addEventListener('change', () => { spec.api_url = urlInput.value; });
    grid.append(el('div', { class: 'admin-source-row' },
      el('label', {},
        cb, el('span', { class: 'admin-source-name', text: id })),
      urlInput,
    ));
  }
  panel.append(grid);
}

// ── Agent routing tab: which sources each agent uses ──
function renderAdminAgentSources(panel) {
  const agentSources = _adminConfig.agent_sources || {};
  // Get the full source list from the sources section
  const allSourceIds = Object.keys(_adminConfig.sources || {}).sort();
  const agents = Object.keys(agentSources).filter(k => !k.startsWith('_')).sort();

  const saveBtn = el('button', { class: 'btn btn-primary', type: 'button',
    onClick: async () => {
      saveBtn.disabled = true;
      try {
        await saveConfigSection('agent_sources', agentSources);
        toast('Agent routing saved', 'ok');
      } catch (e) { toast(e.message, 'error'); }
      finally { saveBtn.disabled = false; }
    },
  }, 'Save routing');

  panel.append(el('p', { class: 'muted small' },
    'Which sources each agent searches. Check a box to include a source ' +
    'in that agent\'s search; uncheck to exclude it.'), saveBtn);

  // Build a matrix: rows = agents, columns = sources
  const table = el('table', { class: 'admin-matrix' });
  // Header row
  const head = el('tr');
  head.append(el('th', { text: 'Agent' }));
  for (const sid of allSourceIds) {
    head.append(el('th', { class: 'admin-matrix-col', text: sid,
      title: sid }));
  }
  table.append(el('thead', {}, head));

  const tbody = el('tbody');
  for (const agent of agents) {
    const row = el('tr');
    row.append(el('td', { class: 'admin-matrix-agent', text: agent }));
    const enabled = new Set(agentSources[agent] || []);
    for (const sid of allSourceIds) {
      const cb = el('input', { type: 'checkbox', checked: enabled.has(sid) });
      cb.addEventListener('change', () => {
        const list = agentSources[agent] || [];
        if (cb.checked) {
          if (!list.includes(sid)) list.push(sid);
        } else {
          agentSources[agent] = list.filter(s => s !== sid);
          return;  // already updated
        }
        agentSources[agent] = list;
      });
      row.append(el('td', { class: 'admin-matrix-cell' }, cb));
    }
    tbody.append(row);
  }
  table.append(tbody);
  panel.append(table);
}

// ── Themes tab: list of themes with add/edit/remove ──
function renderAdminThemes(panel) {
  const themes = _adminConfig.themes || [];
  const saveBtn = el('button', { class: 'btn btn-primary', type: 'button',
    onClick: async () => {
      saveBtn.disabled = true;
      try {
        await saveConfigSection('themes', themes);
        toast('Themes saved', 'ok');
      } catch (e) { toast(e.message, 'error'); }
      finally { saveBtn.disabled = false; }
    },
  }, 'Save themes');

  panel.append(el('p', { class: 'muted small' },
    'The theme bank the Concept Mapper selects from. Each theme has an ID, ' +
    'a label, and keyword seeds. Edit the fields below and click Save to ' +
    'persist changes to config.json.'), saveBtn);

  const list = el('div', { class: 'admin-themes-list' });
  for (let i = 0; i < themes.length; i++) {
    const t = themes[i];
    const kwText = (t.keywords || []).map(k => k.seed || k).join(', ');

    const labelInput = el('input', { type: 'text',
      class: 'admin-theme-label', value: t.label || t.theme_id || '',
      placeholder: 'Display label' });
    labelInput.addEventListener('change', () => {
      t.label = labelInput.value;
    });

    const kwInput = el('input', { type: 'text',
      class: 'admin-theme-keywords', value: kwText,
      placeholder: 'keyword seeds (comma-separated)' });
    kwInput.addEventListener('change', () => {
      const seeds = kwInput.value.split(',').map(s => s.trim()).filter(Boolean);
      t.keywords = seeds.map(s => ({ seed: s, expansion_depth: 1 }));
    });

    const row = el('div', { class: 'admin-theme-row' },
      el('div', { class: 'admin-theme-head' },
        el('span', { class: 'muted small',
          text: `ID: ${t.theme_id}` }),
        el('button', { class: 'btn btn-small btn-danger', type: 'button',
          onClick: async () => {
            themes.splice(i, 1);
            await saveConfigSection('themes', themes);
            renderAdminThemes(panel);
          },
        }, 'Remove'),
      ),
      el('div', { class: 'admin-theme-body' },
        labelInput, kwInput),
    );
    list.append(row);
  }
  panel.append(list);

  // Add theme form — saves immediately on add
  const idInput = el('input', { type: 'text', placeholder: 'theme_id (snake_case)' });
  const labelInput = el('input', { type: 'text', placeholder: 'Display label' });
  const kwInput = el('input', { type: 'text',
    placeholder: 'keyword seeds (comma-separated)' });
  const addBtn = el('button', { class: 'btn btn-small btn-primary', type: 'button',
    onClick: async () => {
      const id = idInput.value.trim();
      if (!id) { toast('Theme ID is required', 'error'); return; }
      // Check for duplicate ID
      if (themes.some(t => t.theme_id === id)) {
        toast(`Theme '${id}' already exists`, 'error');
        return;
      }
      const seeds = kwInput.value.split(',').map(s => s.trim()).filter(Boolean);
      themes.push({
        theme_id: id,
        label: labelInput.value.trim() || id,
        keywords: seeds.map(s => ({ seed: s, expansion_depth: 1 })),
      });
      try {
        await saveConfigSection('themes', themes);
        idInput.value = ''; labelInput.value = ''; kwInput.value = '';
        renderAdminThemes(panel);
      } catch (e) { toast(e.message, 'error'); }
    },
  }, 'Add & save theme');
  panel.append(el('div', { class: 'admin-theme-add' },
    idInput, labelInput, kwInput, addBtn));
}

// ── Templates tab: built-in run templates ──
function renderAdminTemplates(panel) {
  const templates = _adminConfig.run_templates || {};
  const saveBtn = el('button', { class: 'btn btn-primary', type: 'button',
    onClick: async () => {
      saveBtn.disabled = true;
      try {
        await saveConfigSection('run_templates', templates);
        toast('Templates saved', 'ok');
      } catch (e) { toast(e.message, 'error'); }
      finally { saveBtn.disabled = false; }
    },
  }, 'Save templates');

  panel.append(el('p', { class: 'muted small' },
    'Built-in run templates that appear in the New Run template picker. ' +
    'Each template sets default source overrides and a per-source limit.'), saveBtn);

  for (const [name, spec] of Object.entries(templates).sort()) {
    if (!spec || typeof spec !== 'object') continue;
    const descInput = el('input', { type: 'text', value: spec.description || '',
      style: 'width: 100%;' });
    descInput.addEventListener('change', () => { spec.description = descInput.value; });
    const limInput = el('input', { type: 'number', value: spec.limit_per_source || '',
      style: 'width: 80px;' });
    limInput.addEventListener('change', () => {
      spec.limit_per_source = limInput.value ? parseInt(limInput.value) : undefined;
    });
    panel.append(el('div', { class: 'admin-template-row' },
      el('div', { class: 'admin-template-head' },
        el('strong', { text: name }),
        el('button', { class: 'btn btn-small btn-danger', type: 'button',
          onClick: () => {
            delete templates[name];
            renderAdminTemplates(panel);
          },
        }, 'Remove'),
      ),
      el('label', { class: 'field' },
        el('span', { class: 'field-label' }, 'Description'),
        descInput),
      el('label', { class: 'field' },
        el('span', { class: 'field-label' }, 'Limit per source'),
        limInput),
      el('div', { class: 'muted small',
        text: `${Object.keys(spec.source_overrides || {}).length} source overrides` }),
    ));
  }
}

// ── Users tab: view-only user list (admin) ──
// Admins can see which users exist and which providers/sources they have
// configured, but cannot add, edit, or delete credentials for other users.
// Each user manages their own connections from the Settings page.
async function renderAdminUsers(panel) {
  let userList = [];
  try {
    const res = await api('/api/admin/users');
    userList = res.users || [];
  } catch (err) {
    panel.append(el('p', { class: 'error', text: err.message }));
    return;
  }

  if (!userList.length) {
    panel.append(el('p', { class: 'muted', text: 'No users registered yet.' }));
    return;
  }

  panel.append(el('p', { class: 'muted small',
    text: 'View-only. Each user manages their own provider connections from their Settings page.' }));

  // User list table
  const table = el('table', { class: 'admin-user-table' },
    el('thead', {},
      el('tr', {},
        el('th', { text: 'Name' }),
        el('th', { text: 'Auth' }),
        el('th', { text: 'Admin' }),
        el('th', { text: 'Last seen' }),
        el('th', { text: '' }),
      ),
    ),
    el('tbody', {},
      ...userList.map(u => el('tr', {},
        el('td', { text: u.display_name || u.user_id }),
        el('td', { class: 'muted small', text: u.auth_kind }),
        el('td', { text: u.is_admin ? '✓' : '' }),
        el('td', { class: 'muted small',
          text: u.last_seen_at ? u.last_seen_at.slice(0, 10) : '' }),
        el('td', {},
          el('button', {
            class: 'btn btn-small',
            type: 'button',
            onClick: () => showUserConnections(panel, u),
          }, 'View connections')),
      )),
    ),
  );
  panel.append(table);

  // Container for the view-only connections sub-panel
  panel.append(el('div', { id: 'admin-user-credentials', class: 'admin-cred-panel' }));
}

async function showUserConnections(panel, user) {
  const container = $('#admin-user-credentials');
  if (!container) return;
  clear(container);

  // Header
  container.append(el('h3', {},
    `Connections: ${user.display_name || user.user_id}`,
    el('button', {
      class: 'btn btn-ghost btn-small',
      type: 'button',
      style: 'margin-left: 1rem;',
      onClick: () => { clear(container); },
    }, 'Close'),
  ));

  // Load provider credentials (view-only, no secrets)
  let creds = [], srcCreds = [];
  try {
    const [provRes, srcRes] = await Promise.all([
      api(`/api/admin/users/${encodeURIComponent(user.user_id)}/credentials`),
      api(`/api/admin/users/${encodeURIComponent(user.user_id)}/source-credentials`),
    ]);
    creds = provRes.credentials || [];
    srcCreds = srcRes.credentials || [];
  } catch (err) {
    container.append(el('p', { class: 'error', text: err.message }));
    return;
  }

  // Provider connections
  container.append(el('h4', { text: 'Model providers' }));
  if (creds.length) {
    const list = el('div', { class: 'cred-list' });
    for (const c of creds) {
      list.append(el('div', { class: 'cred-item' },
        el('span', { class: 'cred-provider', text: c.provider }),
        el('span', { class: 'muted small', text: c.key_hint || '••••' }),
        el('span', { class: 'muted small',
          text: c.base_url ? c.base_url.slice(0, 40) : '' }),
      ));
    }
    container.append(list);
  } else {
    container.append(el('p', { class: 'muted small',
      text: 'No provider connections configured.' }));
  }

  // Source API keys
  container.append(el('h4', { text: 'Source API keys', style: 'margin-top: 1rem;' }));
  if (srcCreds.length) {
    const list = el('div', { class: 'cred-list' });
    for (const c of srcCreds) {
      list.append(el('div', { class: 'cred-item' },
        el('span', { class: 'cred-provider', text: c.source_id }),
        el('span', { class: 'muted small', text: c.key_hint || '••••' }),
      ));
    }
    container.append(list);
  } else {
    container.append(el('p', { class: 'muted small',
      text: 'No source API keys configured.' }));
  }
}

// ── User settings ──────────────────────────────────────────────────────

function switchSettingsTab(tab) {
  markSelectedTab($$('#settings-tabs .tab'), t => t.dataset.settingsTab === tab);
  $$('#view-settings .tab-panel').forEach(p => { p.hidden = true; });
  const panel = $(`#panel-settings-${tab}`);
  if (panel) { panel.hidden = false; renderSettingsTab(tab, panel); }
}

async function renderSettingsTab(tab, panel) {
  clear(panel);
  if (tab === 'profile')      renderSettingsProfile(panel);
  else if (tab === 'providers') renderSettingsProviders(panel);
  else if (tab === 'sources')   renderSettingsSources(panel);
  else if (tab === 'mcp')       renderSettingsMcp(panel);
  else if (tab === 'preferences') renderSettingsPreferences(panel);
  else {
    // index.html is always refetched, app.js is cached against ?v=N. Forget to
    // bump it and a new tab's button appears while its renderer doesn't exist,
    // leaving a silently blank panel. Say so instead.
    panel.append(el('p', { class: 'muted' },
      `This section needs a newer version of the page. Reload to update.`));
  }
}

/*
 * Preferences tab, rendered from the schema the server sends rather than a
 * hand-built form per field. Adding a key to core/user_settings.py SCHEMA is
 * enough to make it appear here — no default value is duplicated in the client.
 */
async function renderSettingsPreferences(panel) {
  panel.append(el('p', { class: 'muted small', text: 'Loading…' }));

  let data;
  try {
    data = await api('/api/settings');
  } catch (err) {
    clear(panel);
    panel.append(el('p', { class: 'error', text: err.message }));
    return;
  }
  clear(panel);

  state.settings = data.settings;
  const pending = {};

  const control = (spec) => {
    const current = data.settings[spec.key];
    if (spec.type === 'bool') {
      const box = el('input', { type: 'checkbox', id: `set-${spec.key}` });
      box.checked = Boolean(current);
      box.addEventListener('change', () => { pending[spec.key] = box.checked; });
      return box;
    }
    if (spec.type === 'enum') {
      const sel = el('select', { id: `set-${spec.key}` },
        ...spec.choices.map(c => el('option', { value: c, text: c })));
      sel.value = String(current);
      sel.addEventListener('change', () => { pending[spec.key] = sel.value; });
      return sel;
    }
    if (spec.type === 'int' || spec.type === 'float') {
      const inp = el('input', {
        type: 'number', id: `set-${spec.key}`,
        step: spec.type === 'float' ? '0.05' : '1',
        min: spec.min, max: spec.max,
      });
      inp.value = current;
      inp.addEventListener('input', () => { pending[spec.key] = inp.value; });
      return inp;
    }
    if (spec.type === 'list') {
      const inp = el('input', {
        type: 'text', id: `set-${spec.key}`,
        placeholder: 'comma-separated, blank for the default set',
      });
      inp.value = (current || []).join(', ');
      inp.addEventListener('input', () => { pending[spec.key] = inp.value; });
      return inp;
    }
    const inp = el('input', { type: 'text', id: `set-${spec.key}` });
    inp.value = current == null ? '' : String(current);
    inp.addEventListener('input', () => { pending[spec.key] = inp.value; });
    return inp;
  };

  for (const group of data.groups) {
    const section = el('div', { class: 'settings-section' },
      el('h3', { text: group.label }));
    for (const spec of group.settings) {
      const input = control(spec);
      section.append(el('label', { class: 'field', for: `set-${spec.key}` },
        el('span', { class: 'field-label', text: spec.label }),
        input,
        spec.help ? el('span', { class: 'field-hint', text: spec.help }) : null,
      ));
    }
    panel.append(section);
  }

  const saveBtn = el('button', { class: 'btn btn-primary', type: 'button' },
    'Save preferences');
  saveBtn.addEventListener('click', async () => {
    if (!Object.keys(pending).length) { toast('Nothing changed'); return; }
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      const res = await api('/api/settings', { method: 'PUT', body: { settings: pending } });
      state.settings = res.settings;
      applyPreferences(res.settings);
      toast('Preferences saved', 'ok');
      renderSettingsPreferences(panel);      // re-read so coerced values show
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save preferences';
    }
  });
  panel.append(saveBtn);
}

/*
 * Apply the presentation-affecting settings to the live document.
 *
 * Theme still writes through to localStorage so the next first paint doesn't
 * have to wait on /api/settings — the account value is authoritative, the
 * local copy is only there to avoid a flash.
 */
function applyPreferences(settings) {
  if (!settings) return;

  const theme = settings['appearance.theme'] || 'system';
  if (theme === 'system') {
    document.documentElement.removeAttribute('data-theme');
    try { localStorage.removeItem('seeker-theme'); } catch { /* private mode */ }
  } else {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('seeker-theme', theme); } catch { /* private mode */ }
  }

  document.documentElement.setAttribute(
    'data-density', settings['appearance.density'] || 'comfortable');

  const scale = Number(settings['appearance.font_scale']) || 1;
  document.documentElement.style.setProperty('--font-scale', String(scale));

  const perPage = Number(settings['display.runs_per_page']);
  if (perPage) state.runsPageSize = perPage;

  const sort = settings['display.runs_sort'];
  if (sort) {
    runFilters.sort = sort;
    const sel = $('#run-sort');
    if (sel) sel.value = sort;
  }
}

function renderSettingsProfile(panel) {
  const user = state.user;
  if (!user) return;

  // Display name
  const nameInput = el('input', {
    type: 'text', id: 'settings-display-name',
    value: user.display_name || '',
    placeholder: 'Your display name',
  });
  const saveNameBtn = el('button', {
    class: 'btn btn-primary btn-small', type: 'button',
  }, 'Save name');
  saveNameBtn.addEventListener('click', async () => {
    saveNameBtn.disabled = true;
    try {
      await api('/api/auth/me', {
        method: 'PUT',
        body: { display_name: nameInput.value.trim() },
      });
      state.user.display_name = nameInput.value.trim();
      $('#user-chip').textContent = state.user.display_name;
      toast('Display name updated', 'ok');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      saveNameBtn.disabled = false;
    }
  });

  panel.append(el('div', { class: 'settings-section' },
    el('h3', { text: 'Display name' }),
    el('div', { class: 'settings-inline-form' },
      nameInput, saveNameBtn),
  ));

  // Account info
  panel.append(el('div', { class: 'settings-section' },
    el('h3', { text: 'Account' }),
    el('p', { class: 'muted small' },
      `User ID: ${user.user_id}`),
    el('p', { class: 'muted small' },
      `Auth method: ${user.auth_kind || 'provider_key'}`),
    el('p', { class: 'muted small' },
      `Admin: ${user.is_admin ? 'Yes' : 'No'}`),
  ));

  // Change password (password-auth users only)
  if (user.auth_kind === 'password') {
    const currentPw = el('input', {
      type: 'password', id: 'settings-current-pw',
      placeholder: 'Current password', autocomplete: 'current-password',
    });
    const newPw = el('input', {
      type: 'password', id: 'settings-new-pw',
      placeholder: 'New password (min 8 chars)', autocomplete: 'new-password',
    });
    const newPw2 = el('input', {
      type: 'password', id: 'settings-new-pw2',
      placeholder: 'Confirm new password', autocomplete: 'new-password',
    });
    const changeBtn = el('button', {
      class: 'btn btn-primary btn-small', type: 'button',
    }, 'Change password');
    changeBtn.addEventListener('click', async () => {
      if (newPw.value !== newPw2.value) {
        toast('New passwords do not match', 'error'); return;
      }
      if (newPw.value.length < 8) {
        toast('Password must be at least 8 characters', 'error'); return;
      }
      changeBtn.disabled = true;
      changeBtn.textContent = 'Changing…';
      try {
        await api('/api/auth/password', {
          method: 'PUT',
          body: {
            current_password: currentPw.value,
            new_password: newPw.value,
          },
        });
        currentPw.value = ''; newPw.value = ''; newPw2.value = '';
        toast('Password changed', 'ok');
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        changeBtn.disabled = false;
        changeBtn.textContent = 'Change password';
      }
    });

    panel.append(el('div', { class: 'settings-section' },
      el('h3', { text: 'Change password' }),
      el('label', { class: 'field' },
        el('span', { class: 'field-label' }, 'Current password'),
        currentPw),
      el('label', { class: 'field' },
        el('span', { class: 'field-label' }, 'New password'),
        newPw),
      el('label', { class: 'field' },
        el('span', { class: 'field-label' }, 'Confirm new password'),
        newPw2),
      changeBtn,
    ));
  }
}

async function renderSettingsProviders(panel) {
  clear(panel);            // re-invoked after save/delete — see renderSettingsMcp
  // Load current credentials
  let creds = [];
  try {
    const res = await api('/api/credentials');
    creds = res.credentials || [];
  } catch (err) {
    panel.append(el('p', { class: 'error', text: err.message }));
    return;
  }

  panel.append(el('p', { class: 'muted small',
    text: 'Your model provider connections are private to your account. The API key is validated against the provider before being stored encrypted.' }));

  // Existing credentials
  if (creds.length) {
    const list = el('div', { class: 'cred-list' });
    for (const c of creds) {
      list.append(el('div', { class: 'cred-item' },
        el('span', { class: 'cred-provider', text: c.provider }),
        el('span', { class: 'muted small', text: c.key_hint || '••••' }),
        el('span', { class: 'muted small',
          text: c.base_url ? c.base_url.slice(0, 40) : '' }),
        el('button', {
          class: 'btn btn-ghost btn-small',
          type: 'button',
          onClick: () => deleteProviderCredential(c.provider, panel),
        }, 'Remove'),
      ));
    }
    panel.append(list);
  } else {
    panel.append(el('p', { class: 'muted',
      text: 'No provider connections configured. Add one below.' }));
  }

  // Add credential form
  panel.append(el('div', { class: 'divider' }, el('span', { text: 'Add provider' })));

  const providerSelect = el('select', { id: 'settings-cred-provider' },
    ...Object.keys(BASE_URL_DEFAULTS).map(p =>
      el('option', { value: p, text: p })),
  );
  const baseUrlInput = el('input', {
    type: 'url', id: 'settings-cred-base-url',
    value: BASE_URL_DEFAULTS['open-webui'] || '',
    placeholder: 'http://localhost:3000/api',
  });
  const apiKeyInput = el('input', {
    type: 'password', id: 'settings-cred-api-key',
    placeholder: 'sk-…', autocomplete: 'off',
  });

  providerSelect.addEventListener('change', () => {
    const preset = BASE_URL_DEFAULTS[providerSelect.value];
    if (preset) baseUrlInput.value = preset;
  });

  const addBtn = el('button', {
    class: 'btn btn-primary btn-small', type: 'button',
  }, 'Add connection');
  addBtn.addEventListener('click', async () => {
    addBtn.disabled = true;
    addBtn.textContent = 'Validating…';
    try {
      await api('/api/credentials', {
        method: 'PUT',
        body: {
          provider: providerSelect.value,
          base_url: baseUrlInput.value.trim(),
          api_key: apiKeyInput.value.trim(),
          models: {},
        },
      });
      apiKeyInput.value = '';
      toast('Provider connection added', 'ok');
      // Refresh credentials in state and re-render
      await refreshUserCredentials();
      renderSettingsProviders(panel);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      addBtn.disabled = false;
      addBtn.textContent = 'Add connection';
    }
  });

  panel.append(
    el('div', { class: 'admin-cred-form' },
      el('label', { class: 'field' },
        el('span', { class: 'field-label' }, 'Provider'),
        providerSelect),
      el('label', { class: 'field' },
        el('span', { class: 'field-label' }, 'Base URL'),
        baseUrlInput),
      el('label', { class: 'field' },
        el('span', { class: 'field-label' }, 'API key'),
        apiKeyInput),
      addBtn,
    ),
  );
}

async function deleteProviderCredential(provider, panel) {
  try {
    await api(`/api/credentials/${encodeURIComponent(provider)}`,
      { method: 'DELETE' });
    toast(`Removed ${provider} connection`, 'ok');
    await refreshUserCredentials();
    renderSettingsProviders(panel);
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function refreshUserCredentials() {
  try {
    const me = await api('/api/auth/me');
    state.user = me;
    state.providers = me.credentials || [];
  } catch { /* ignore */ }
}

async function renderSettingsSources(panel) {
  clear(panel);            // re-invoked after save/delete — see renderSettingsMcp
  const keyable = ['scopus', 'semantic_scholar', 'core', 'google_books',
                   'philpapers', 'openalex', 'pubmed', 'primo'];

  panel.append(el('p', { class: 'muted small',
    text: 'Store a per-user API key for sources that require authentication (Scopus, CORE, Semantic Scholar, Primo library catalog, etc.). Keys are stored encrypted and used instead of the matching .env variable for your runs. For Primo, also set PRIMO_VID, PRIMO_SCOPE, and PRIMO_TAB in .env — these are institution-specific and shared across users.' }));

  // Load existing source keys
  let creds = [];
  try {
    const res = await api('/api/source-credentials');
    creds = res.credentials || [];
  } catch (err) {
    panel.append(el('p', { class: 'error', text: err.message }));
    return;
  }

  // Existing source keys
  if (creds.length) {
    const list = el('div', { class: 'cred-list' });
    for (const c of creds) {
      list.append(el('div', { class: 'cred-item' },
        el('span', { class: 'cred-provider', text: c.source_id }),
        el('span', { class: 'muted small', text: c.key_hint || '••••' }),
        el('button', {
          class: 'btn btn-ghost btn-small',
          type: 'button',
          onClick: () => deleteSourceKeyFromSettings(c.source_id, panel),
        }, 'Remove'),
      ));
    }
    panel.append(list);
  } else {
    panel.append(el('p', { class: 'muted',
      text: 'No source API keys configured. Add one below.' }));
  }

  // Add source key form
  panel.append(el('div', { class: 'divider' }, el('span', { text: 'Add source key' })));

  const sourceSelect = el('select', { id: 'settings-src-source' },
    ...keyable.map(s => el('option', { value: s, text: s })),
  );
  const keyValueInput = el('input', {
    type: 'password', id: 'settings-src-key',
    placeholder: 'API key', autocomplete: 'off',
  });
  const saveBtn = el('button', {
    class: 'btn btn-primary btn-small', type: 'button',
  }, 'Save key');
  saveBtn.addEventListener('click', async () => {
    if (!keyValueInput.value) return;
    saveBtn.disabled = true;
    try {
      await api('/api/source-credentials', {
        method: 'PUT',
        body: { source_id: sourceSelect.value, api_key: keyValueInput.value },
      });
      keyValueInput.value = '';
      toast(`Key for ${sourceSelect.value} saved`, 'ok');
      renderSettingsSources(panel);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      saveBtn.disabled = false;
    }
  });

  panel.append(
    el('div', { class: 'settings-inline-form' },
      sourceSelect, keyValueInput, saveBtn),
  );
}

async function deleteSourceKeyFromSettings(sourceId, panel) {
  try {
    await api(`/api/source-credentials/${encodeURIComponent(sourceId)}`,
      { method: 'DELETE' });
    toast(`Key for ${sourceId} removed`, 'ok');
    renderSettingsSources(panel);
  } catch (err) {
    toast(err.message, 'error');
  }
}

function showSettings() {
  showView('settings');
  switchSettingsTab('profile');
}

async function renderSettingsMcp(panel) {
  // These renderers append, and each is re-invoked from its own action
  // handlers as well as from renderSettingsTab. Only the latter cleared first,
  // so acting on a row stacked a second copy of the whole tab beneath it.
  // Clearing here makes the function idempotent whoever calls it.
  clear(panel);
  panel.append(el('p', { class: 'muted small',
    text: 'Globally enable or disable MCP-based connections for your runs. ' +
          'When disabled, the pipeline skips that connection entirely — ' +
          'no API calls are made, no tokens consumed. Toggles apply to all ' +
          'current and future runs.' }));

  let connections = [];
  try {
    const res = await api('/api/mcp-toggles');
    connections = res.connections || [];
  } catch (err) {
    panel.append(el('p', { class: 'error', text: err.message }));
    return;
  }

  if (!connections.length) {
    panel.append(el('p', { class: 'muted',
      text: 'No MCP connections configured.' }));
    return;
  }

  const list = el('div', { class: 'mcp-toggle-list' });
  for (const conn of connections) {
    const item = el('div', { class: 'mcp-toggle-item' });

    const badge = el('span', {
      class: `mcp-status-badge ${conn.enabled ? 'mcp-status-on' : 'mcp-status-off'}`,
      text: conn.enabled ? 'Enabled' : 'Disabled',
    });

    const box = el('input', {
      type: 'checkbox',
      checked: conn.enabled,
      'aria-label': `Enable ${conn.label}`,
    });
    box.addEventListener('change', () =>
      toggleMcpConnection(conn.conn_id, box.checked, box, badge));

    item.append(el('label', { class: 'switch' },
      box, el('span', { class: 'switch-slider' })));

    // Label and description
    const info = el('div', { class: 'mcp-toggle-info' });
    info.append(el('div', { class: 'mcp-toggle-label', text: conn.label }));
    info.append(el('div', { class: 'muted small', text: conn.description }));
    item.append(info);
    item.append(badge);

    list.append(item);
  }
  panel.append(list);
}

/*
 * Update the one row that changed rather than re-rendering the tab. A full
 * re-render also refetched the list and threw away scroll position for a
 * single boolean.
 */
async function toggleMcpConnection(connId, enabled, box, badge) {
  box.disabled = true;
  try {
    await api('/api/mcp-toggles', {
      method: 'PUT',
      body: { conn_id: connId, enabled },
    });
    badge.textContent = enabled ? 'Enabled' : 'Disabled';
    badge.className =
      `mcp-status-badge ${enabled ? 'mcp-status-on' : 'mcp-status-off'}`;
    toast(`${connId} ${enabled ? 'enabled' : 'disabled'}`, 'ok');
  } catch (err) {
    // The write failed, so put the switch back where it was.
    box.checked = !enabled;
    toast(err.message, 'error');
  } finally {
    box.disabled = false;
  }
}

/* Arrow-key navigation within a tablist. Expected of anything using the tab
 * role, and the reason the roving tabindex in markSelectedTab exists: Tab
 * moves past the whole strip, arrows move between the tabs in it. */
function wireTablistKeys(nav, onSelect, attr) {
  nav.addEventListener('keydown', ev => {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!keys.includes(ev.key)) return;
    const tabs = Array.from(nav.querySelectorAll('.tab')).filter(t => !t.hidden);
    if (!tabs.length) return;
    const current = tabs.indexOf(document.activeElement);
    let next;
    if (ev.key === 'Home') next = 0;
    else if (ev.key === 'End') next = tabs.length - 1;
    else {
      const step = ev.key === 'ArrowRight' ? 1 : -1;
      next = ((current < 0 ? 0 : current) + step + tabs.length) % tabs.length;
    }
    ev.preventDefault();
    tabs[next].focus();
    onSelect(tabs[next].dataset[attr]);
  });
}

function wireChrome() {
  const runTabs = $('#run-tabs');
  runTabs.addEventListener('click', ev => {
    const tab = ev.target.closest('.tab');
    if (tab) switchTab(tab.dataset.tab);
  });
  wireTablistKeys(runTabs, switchTab, 'tab');
  $$('[data-nav="runs"]').forEach(node => {
    node.addEventListener('click', () => showRuns().catch(err => toast(err.message, 'error')));
    // The brand is a div with role="button"; a real button responds to Enter
    // and Space, so one carrying the role has to as well.
    if (node.getAttribute('role') === 'button') {
      node.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); node.click(); }
      });
    }
  });
  // F12: admin button + admin tab switching
  $('#btn-admin')?.addEventListener('click', () => showAdmin().catch(err => toast(err.message, 'error')));
  $('#btn-guide')?.addEventListener('click', () => showView('guide'));
  // Settings button + settings tab switching
  $('#btn-settings')?.addEventListener('click', () => showSettings());
  const linkSettingsSources = $('#link-settings-sources');
  linkSettingsSources?.addEventListener('click', ev => {
    ev.preventDefault();
    showSettings();
    switchSettingsTab('sources');
  });
  const settingsTabs = $('#settings-tabs');
  settingsTabs?.addEventListener('click', ev => {
    const tab = ev.target.closest('[data-settings-tab]');
    if (tab) switchSettingsTab(tab.dataset.settingsTab);
  });
  if (settingsTabs) wireTablistKeys(settingsTabs, switchSettingsTab, 'settingsTab');
  const adminTabs = $('#admin-tabs');
  adminTabs?.addEventListener('click', ev => {
    const tab = ev.target.closest('[data-admin-tab]');
    if (tab) switchAdminTab(tab.dataset.adminTab);
  });
  if (adminTabs) wireTablistKeys(adminTabs, switchAdminTab, 'adminTab');
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
    // Which database backend is running is operator information. Stash it now;
    // afterSignIn() decides whether this user should see it.
    state.storage = health.storage;
    if (!health.secrets_configured) {
      toast('Server has no SEEKER_SECRET_KEY — sign-in will be refused.', 'error');
    }
  } catch { /* health is advisory */ }

  // Discover available auth methods (shows SSO button if SAML is configured)
  discoverAuthMethods();

  try {
    await afterSignIn();          // an existing session cookie signs us straight in
  } catch {
    showView('login');
  }
}

document.addEventListener('DOMContentLoaded', init);
