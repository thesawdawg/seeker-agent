import { api } from '../core/api-client.js?v=24';
import { $, $$, clear, el } from '../core/dom.js?v=24';
import { showView } from '../core/navigation.js?v=24';
import { state, updateState } from '../core/store.js?v=24';
import { markSelectedTab } from '../components/tabs.js?v=24';
import { toast } from '../components/toast.js?v=24';
import { BASE_URL_DEFAULTS } from './auth.js?v=24';
import { runFilters, runsPageSize } from './runs.js?v=24';

// ── User settings ──────────────────────────────────────────────────────

export function switchSettingsTab(tab) {
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
    // index.html is always refetched, while scripts are cached against ?v=N.
    // Forget to bump them and a new tab's button can appear without its
    // renderer, leaving a silently blank panel. Say so instead.
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

  updateState({ settings: data.settings }, 'settings:loaded');
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
      updateState({ settings: res.settings }, 'settings:saved');
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
export function applyPreferences(settings) {
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
  if (perPage) {
    updateState({ runsPageSize: perPage }, 'settings:display-applied');
  }

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
    const healthIndicators = new Map();
    for (const c of creds) {
      const health = el('span', {
        class: 'provider-health is-checking',
        role: 'status',
        'aria-label': `${c.provider} health: checking`,
      },
      el('span', {
        class: 'provider-health-dot',
        'aria-hidden': 'true',
      }),
      el('span', { text: 'Checking…' }));
      healthIndicators.set(c.provider, health);
      list.append(el('div', { class: 'cred-item' },
        el('span', { class: 'cred-provider', text: c.provider }),
        el('span', { class: 'muted small', text: c.key_hint || '••••' }),
        el('span', { class: 'muted small',
          text: c.base_url ? c.base_url.slice(0, 40) : '' }),
        health,
        el('button', {
          class: 'btn btn-ghost btn-small',
          type: 'button',
          onClick: () => deleteProviderCredential(c.provider, panel),
        }, 'Remove'),
      ));
    }
    panel.append(list);
    void checkProviderHealth(healthIndicators);
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

async function checkProviderHealth(indicators) {
  let providers;
  try {
    const result = await api('/api/providers/health');
    providers = result.providers || [];
  } catch (err) {
    for (const [provider, indicator] of indicators) {
      updateProviderHealthIndicator(
        indicator, provider, 'unavailable', 'Health check failed');
    }
    return;
  }

  const results = new Map(providers.map(item => [item.provider, item]));
  for (const [provider, indicator] of indicators) {
    const result = results.get(provider) || {
      status: 'unavailable',
      message: 'No health result returned',
      models_count: 0,
    };
    const modelNote = result.status === 'ok'
      ? ` · ${result.models_count} model${result.models_count === 1 ? '' : 's'}`
      : '';
    updateProviderHealthIndicator(
      indicator, provider, result.status, `${result.message}${modelNote}`);
  }
}

function updateProviderHealthIndicator(indicator, provider, status, message) {
  indicator.className = `provider-health is-${status}`;
  indicator.setAttribute('aria-label', `${provider} health: ${message}`);
  clear(indicator);
  indicator.append(
    el('span', {
      class: 'provider-health-dot',
      'aria-hidden': 'true',
    }),
    el('span', { text: message }),
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
    updateState({
      user: me,
      providers: me.credentials || [],
    }, 'auth:credentials-refreshed');
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

export function showSettings() {
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
