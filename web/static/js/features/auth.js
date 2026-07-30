import { api } from '../core/api-client.js?v=22';
import { $ } from '../core/dom.js?v=22';
import { showView } from '../core/navigation.js?v=22';
import { state, updateState } from '../core/store.js?v=22';
import { showRuns } from './runs.js?v=22';
import { applyPreferences } from './settings.js?v=22';

/* ── sign in ─────────────────────────────────────────────────────────── */

export const BASE_URL_DEFAULTS = {
  'open-webui': 'http://localhost:3000/api',
  'openai':     'https://api.openai.com/v1',
  'ollama':     'http://localhost:11434/v1',
  'lm-studio':  'http://localhost:1234/v1',
  'vllm':       'http://localhost:8000/v1',
  'openrouter': 'https://openrouter.ai/api/v1',
  'anthropic':  'https://api.anthropic.com',
};

export function wireLogin() {
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
    updateState({ user: null }, 'auth:logged-out');
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
export async function discoverAuthMethods() {
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

export async function afterSignIn() {
  const me = await api('/api/auth/me');
  updateState({
    user: me,
    providers: me.credentials || [],
  }, 'auth:signed-in');
  $('#user-chip').textContent = me.display_name || me.user_id;

  const [shape, agents] = await Promise.all([
    api('/api/steps'), api('/api/agents'),
  ]);
  updateState({
    steps: shape.steps,
    agents: agents.agents,
  }, 'app:metadata-loaded');

  // F12: check admin status to show/hide the Admin button
  try {
    const who = await api('/api/config/whoami');
    updateState({ isAdmin: who.is_admin }, 'auth:admin-resolved');
    $('#btn-admin').hidden = !who.is_admin;
  } catch {
    updateState({ isAdmin: false }, 'auth:admin-unavailable');
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
    updateState({ settings: prefs.settings }, 'settings:loaded');
    applyPreferences(prefs.settings);
  } catch { /* defaults are fine */ }

  await loadModels();
  await showRuns();
}

export async function loadModels() {
  const provider = (state.providers[0] || {}).provider;
  if (!provider) {
    updateState({ models: [] }, 'models:cleared');
    return;
  }
  try {
    const body = await api(`/api/models?provider=${encodeURIComponent(provider)}`);
    updateState({ models: body.models || [] }, 'models:loaded');
  } catch {
    updateState({ models: [] }, 'models:unavailable');
  }
}
