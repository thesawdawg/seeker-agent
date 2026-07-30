import { api } from '../core/api-client.js?v=22';
import { $, $$, clear, el } from '../core/dom.js?v=22';
import { showView } from '../core/navigation.js?v=22';
import { state } from '../core/store.js?v=22';
import { markSelectedTab } from '../components/tabs.js?v=22';
import { toast } from '../components/toast.js?v=22';

/* ── admin / config editor (F12) ─────────────────────────────────────── */

let _adminConfig = null;  // cached config from GET /api/config

// Save one config section (F12). Used by all the admin tab save buttons.
async function saveConfigSection(section, value) {
  await api(`/api/config/sections/${section}`, {
    method: 'PUT', body: { value },
  });
  toast(`${section} saved`, 'ok');
}

export async function showAdmin() {
  if (!state.isAdmin) { toast('Admin access required', 'error'); return; }
  showView('admin');
  _adminConfig = await api('/api/config');
  switchAdminTab('sources');
}

export function switchAdminTab(tab) {
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
