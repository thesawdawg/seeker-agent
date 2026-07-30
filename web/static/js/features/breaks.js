import { api } from '../core/api-client.js?v=22';
import { $, clear, el } from '../core/dom.js?v=22';
import { state, updateState } from '../core/store.js?v=22';
import { toast } from '../components/toast.js?v=22';
import {
  buildModelGrid,
  collectModelOverrides,
  collectSourceOverrides,
} from './new-run.js?v=22';
import { switchTab } from './results.js?v=22';
import { refreshStatus, startPolling } from './run-detail.js?v=22';
import { buildDirectives } from './break-directives.js?v=22';

/* ── break screens ───────────────────────────────────────────────────── */

export async function openBreak(breakNum) {
  const payload = await api(`/api/runs/${state.runId}/break/${breakNum}`);
  updateState({ breakDraft: {
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
  } }, 'break:draft-opened');
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
    updateState({ breakDraft: null }, 'break:submitted');
    switchTab('overview');
    await refreshStatus();
    startPolling();
  } catch (err) {
    toast(err.message, 'error');
    button.disabled = false;
    button.textContent = 'Submit and continue';
  }
}
