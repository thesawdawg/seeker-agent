import { api } from './core/api-client.js?v=24';
import { $, $$ } from './core/dom.js?v=24';
import { on } from './core/events.js?v=24';
import { showView } from './core/navigation.js?v=24';
import { state, updateState } from './core/store.js?v=24';
import { wireModal } from './components/modal.js?v=24';
import { wireTablistKeys } from './components/tabs.js?v=24';
import { toast } from './components/toast.js?v=24';
import { showAdmin, switchAdminTab } from './features/admin.js?v=24';
import {
  afterSignIn,
  discoverAuthMethods,
  wireLogin,
} from './features/auth.js?v=24';
import { wireNewRun } from './features/new-run.js?v=24';
import { switchTab } from './features/results.js?v=24';
import { startPolling, stopPolling } from './features/run-detail.js?v=24';
import { showRuns } from './features/runs.js?v=24';
import {
  showSettings,
  switchSettingsTab,
} from './features/settings.js?v=24';

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
  wireModal();
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
    updateState({ storage: health.storage }, 'app:health-loaded');
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

on('session:expired', () => showView('login'));

document.addEventListener('DOMContentLoaded', init);
