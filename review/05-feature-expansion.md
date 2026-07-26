# Feature Expansions — UX & Research Returns

These are not corrections to existing defects; they are additions that would meaningfully improve either the researcher's experience or the quality of the research output. Ordered by value/effort ratio.

## F1 — Per-source result preview before Break 0

**Value: High · Effort: M**

Today, Break 0 shows the concept mapper's activated themes as checkboxes. The researcher confirms themes blind — they don't see what each theme will actually return until the full Social/Grounder run completes minutes later. If a theme returns 0 results, the researcher finds out at Break 1 and has to re-run from grounder.

**Proposal:** add a "Preview" button next to each theme on the Break 0 screen that fires a single OpenAlex + Semantic Scholar query (2 sources, 3 results each) and shows the top titles inline. The researcher can confirm "yes, this theme has coverage" or remove the theme before the full search runs. Cost: ~2 API calls per previewed theme, well within rate limits. Implement as `POST /api/runs/{id}/break/0/preview?theme=<id>` returning a small JSON payload.

## F2 — Source coverage matrix in the Understanding Map

**Value: High · Effort: S**

The Understanding Map (Scribe output) should open with a coverage matrix: which sources contributed, how many sources per theme, which themes had thin coverage. Today the researcher has to infer this from the references section. This is the single biggest trust signal for an LLM-generated research map — "this map is built from 142 sources across 8 themes; theme X had only 3 sources, treat its claims with caution."

**Proposal:** Scribe already receives the manifest (`references.py:build_manifest`). Extend the Scribe prompt to include a per-theme, per-source count table and instruct it to write a "Coverage and Confidence" preamble. Backend change is one prompt extension; no schema changes.

## F3 — Incremental / streaming run updates via SSE

**Value: Medium · Effort: M**

The UI polls every 2s. For a 20-minute run that's 600 polls, each hitting the DB for step state. Server-Sent Events would push updates only when state changes (step starts, activity note, step completes, break arrives). The infrastructure is already there — `progress.note()` writes to the DB on every activity; an SSE endpoint can `LISTEN` on a Postgres/MySQL channel (or poll internally at 1s and dedupe) and push to the browser.

**Proposal:** add `GET /api/runs/{id}/events` as an SSE stream. The frontend switches from `setTimeout(tick)` to `EventSource` when available, falling back to polling for older browsers. Reduces DB load by ~10× and gives sub-second activity updates instead of 2s lag.

## F4 — Run templates / presets

**Value: Medium · Effort: S**

A humanities researcher and a CS researcher want different defaults: different sources, different `limit_per_source`, different model roles (humanities may want a larger primary model for narrative coherence; CS may want a faster one for iterative searches). Today everyone starts from the same `config.json` defaults.

**Proposal:** add a "Template" dropdown on the New Run screen. Templates are stored in `config.json` under a new `run_templates` key: `{name, sources, limit_per_source, model_roles, agent_overrides}`. Three ship by default: "Humanities", "CS / Quantitative", "Quick scan" (low limits, fast). Users can save a run's settings as a new template (U10's config export, persisted).

## F5 — Cross-run source deduplication and "already-seen" tracking

**Value: Medium · Effort: M**

A researcher running multiple related problems will see the same seminal sources come back in every run (Freire, Foucault, etc. for identity work). The Understanding Map re-introduces them every time. More usefully, a researcher running a *follow-up* run wants to know "what's new since my last run on this topic?"

**Proposal:** `sources` table already has `run_id`. Add a "Compare to run" picker on the New Run screen that, when set, passes `previous_run_id` to Grounder/Social. The agents exclude sources already in the previous run from the synthesis (or flag them as "previously seen" rather than re-introducing). The Understanding Map then opens with "12 new sources since run RUN-... ; 8 previously seen sources confirmed."

## F6 — Per-step artifact download (not just Scribe)

**Value: Medium · Effort: S**

Only Scribe outputs are exposed under `/api/runs/{id}/artifacts`. But every agent writes a markdown doc to `artifacts/` (`historian.py:287`, `grounder.py`'s foundations doc, etc.). A researcher who wants to read the Historian's full chronological map — not just the Scribe's summary of it — has no UI path.

**Proposal:** generalize the artifacts endpoint to list all files in `artifacts/` matching `{run_id}_*.md`. Group by step. The UI's Artifacts tab shows them all, with the step name as a label.

## F7 — Collaborative breaks (multiple reviewers)

**Value: Medium · Effort: L**

A research team running a pipeline might want two researchers to review Break 1 independently and reconcile. Today breaks are single-user: one cookie, one submission.

**Proposal:** add a "Share break" link that generates a read-only URL (signed token) for a break. A second reviewer can view the break payload and add comments (appended to the free-text field as "Reviewer B: ..."). The primary submitter sees the comments and incorporates them. This is a larger feature but maps to how research actually happens in teams.

## F8 — Source blacklist / "don't cite this"

**Value: Medium · Effort: S**

A researcher may know that a particular source is retracted, predatory, or just wrong for their field. Today there's no way to say "never include source X" — the pipeline will happily surface it if it matches the query.

**Proposal:** add a `source_blacklist` table keyed by `(user_id, doi|url|title_substring)`. The source handlers check each result against the blacklist before inserting. The UI exposes it as a per-run "Excluded sources" list on the New Run screen and a global "Blacklist" page in settings.

## F9 — Argument tree visualization

**Value: High · Effort: L**

The argument tree (`core/argument_tree.py`, 684 lines) is the "backbone" of the pipeline per the README, but the UI never shows it. The tree is the most natural way to understand *why* a claim appears in the Understanding Map — trace it back through questions → claims → evidence.

**Proposal:** add a "Tree" tab on the run view that renders the argument tree as an interactive graph (D3 or a lightweight tree library). Nodes are color-coded by type (root/question/claim/evidence/counter/historical/external) and by audit status (solid/contested/weak/unsupported from the Historian's audit). Clicking a node shows its metadata and source. This is the highest-value visualization for a researcher trying to trust the output.

## F10 — Per-agent token usage and cost tracking

**Value: Medium · Effort: M**

The pipeline makes many LLM calls per run (decomposition, query gen, synthesis, relevance rating, semantic citation checks). There's no visibility into which agent spent how many tokens, or what the run cost against a paid provider.

**Proposal:** `llm.py:_post_openai` already has the response — extend it to extract `usage` from the JSON and write a `llm_usage` row: `(run_id, agent_name, provider, model, prompt_tokens, completion_tokens, timestamp)`. New endpoint `GET /api/runs/{id}/usage` returns per-agent and per-model totals. UI shows it on the run's Overview tab. For paid providers, optional cost estimation via a `price_per_1k` config field.

## F11 — "Continue from here" — branch a run

**Value: Medium · Effort: M**

A researcher completes a run, reads the Understanding Map, and wants to explore one specific gap or proposal in depth. Today they start a new run with a new problem statement and lose the tree, the sources, and the context.

**Proposal:** add a "Branch from step" action that clones a run's state up to a chosen step (including sources, tree, gaps) into a new run with a new `run_id`, then lets the researcher edit the problem or add directives. The new run starts from that step with the cloned context. This is the natural extension of the existing re-run machinery (`pipeline.reset_step`), generalized to clone instead of reset.

## F12 — Web-based `config.json` editor for operators

**Value: Low · Effort: M**

An operator (vs. researcher) who wants to add a new theme, change `agent_sources`, or tune rate limits has to edit `config.json` on the server. For a self-hosted deployment this is fine; for a hosted multi-user deployment it's not.

**Proposal:** admin-only endpoints (`/api/admin/config`) that read and write `config.json` with a structured schema editor UI. Restricted to users flagged as admins in the `users` table. Out of scope for a single-researcher install, but necessary for the multi-user path the README already targets.

---

## Recommended priority

If only a few of these land, the highest value-to-effort ratio is:

1. **F2** (coverage matrix in Understanding Map) — S effort, immediate trust improvement.
2. **F1** (per-source preview at Break 0) — M effort, prevents wasted runs.
3. **F9** (argument tree visualization) — L effort but the single most valuable visualization for a research tool.
4. **F10** (token/cost tracking) — M effort, essential for paid-provider users.
5. **F5** (cross-run dedup) — M effort, enables iterative research workflows.

F3 (SSE) and F4 (templates) are quality-of-life improvements that pair naturally with the Area-1 optimization work — both reduce wasted requests.
