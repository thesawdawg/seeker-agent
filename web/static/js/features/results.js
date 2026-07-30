import { api } from '../core/api-client.js?v=24';
import { $, $$, clear, el, shortTime } from '../core/dom.js?v=24';
import { mdToElement } from '../core/markdown.js?v=24';
import { state, updateState } from '../core/store.js?v=24';
import { markSelectedTab } from '../components/tabs.js?v=24';
import { toast } from '../components/toast.js?v=24';
import { openBreak } from './breaks.js?v=24';
import { renderOverview } from './run-detail.js?v=24';

/* ── artifacts ───────────────────────────────────────────────────────── */

export async function renderArtifacts() {
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

export function switchTab(name) {
  updateState({ tab: name }, 'navigation:run-tab-changed');
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
