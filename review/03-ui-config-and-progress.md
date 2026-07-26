# Area 3 — User Configuration Control & UI Progress

The UI is well-built for what it exposes: model selection, break directives, re-run with impact preview, stop/resume. The gap is that **almost nothing else about the pipeline is exposed**. Every knob that controls *what data the run produces* — which sources, how many results per source, which themes, per-agent token budgets — lives in `config.json` and requires a file edit + redeploy. A researcher using the web UI cannot tune their own run.

## U1 — No source enable/disable control in the UI

**Severity: High · Effort: M**

`config.json:1186` defines `agent_sources.social` and `agent_sources.grounder` as static lists. A researcher who knows their institution has Scopus access and wants to add it for one run, or who wants to drop arXiv because their problem is humanities-only, has to edit `config.json` and restart the worker. This is the most common per-run tuning need and it's invisible to the web user.

**Fix:**
- Add a `sources` field to `CreateRunRequest` (`web/app.py:88`) and `BreakSubmission` — a list of `{source: "scopus", enabled: true}` overrides.
- Store per-run source overrides in a new `run_source_overrides` table (same pattern as `model_overrides`).
- The worker's `_register_user_providers` (`worker.py:52`) already loads per-run config; add a step that merges `run_source_overrides` into the `config` dict before passing it to `pipeline.advance`.
- UI: on the New Run screen, add a "Sources" disclosure under "Model routing" listing every source from `config.sources` with a checkbox (default = the agent_sources default). Group by agent (Social / Grounder / Historian).

## U2 — No per-source API key management in the UI

**Severity: High · Effort: M**

The UI manages LLM provider credentials (`PUT /api/credentials`) but has no equivalent for academic source keys: `SCOPUS_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `CORE_API_KEY`, `PHILPAPERS_API_ID`/`KEY`, `NCBI_API_KEY`/`EMAIL`, `GOOGLE_BOOKS_API_KEY`. These are env vars today (`core/keys.py`). A multi-user deployment can't have every user's Scopus key in one `.env` file.

**Fix:**
- Extend the credentials system: `users.set_credentials` already stores `(user_id, provider, base_url, api_key)`. Generalize `provider` to include academic sources, or add a parallel `source_credentials` table.
- `core/keys.py` accessors (`scopus_api_key()`, etc.) gain a `user_id` parameter and check the DB first, then env. The worker passes the run owner's `user_id` through to the handler.
- UI: a "Source keys" section on the credentials page, with the same masked-hint display the LLM credentials use.

## U3 — No visibility into source health or coverage during/after a run

**Severity: High · Effort: M**

The live card (`app.js:556`) shows the current step and the latest activity line. The activity log (`app.js:798`) shows a flat list of "Semantic Scholar — searching" events. There is no view that shows:

- Which sources were configured for this run vs. which actually returned data.
- Per-source result counts ("OpenAlex: 42, arXiv: 15, Semantic Scholar: 0").
- Per-source health ("Semantic Scholar: failed after 3 retries, 503").
- How many API calls were made against each source vs. the daily limit.

This is the information a researcher needs to judge whether the Understanding Map at the end is trustworthy. Today the only signal is the step's pass/fail status.

**Fix:**
- Add a `source_health` table: `(run_id, source_id, status, results_returned, calls_made, retries, last_error, checked_at)`. Each handler writes a row after its search completes (success or failure).
- New endpoint `GET /api/runs/{id}/sources` returning per-source stats.
- UI: a "Sources" tab on the run view (alongside Overview / Break / Artifacts) showing a table of sources, their status, result count, and any errors. Update on each poll when `progress.done` advances.

## U4 — No per-agent `max_tokens` / `temperature` control in the UI

**Severity: Medium · Effort: S**

`config.json:llm.agents.grounder.max_tokens = 16000` is a file edit. A researcher who wants Grounder to produce a longer synthesis, or wants Theorist to run at temperature 0.9 for more creative proposals, can't do it from the UI. The model-override grid (`app.js:243`) only exposes model *name*.

**Fix:** extend `buildModelGrid` to render `max_tokens` (number input) and `temperature` (slider 0–1) per agent, alongside the model select. Extend `collectModelOverrides` to include them. The backend `set_run_overrides` (`llm.py:312`) already accepts these fields.

## U5 — No `limit_per_source` control

**Severity: Medium · Effort: S**

`social.py:_collect_for_theme` hardcodes `limit_per_source=8` (`social.py:1057`). `grounder.py` hardcodes `limit=4`/`3` per source (`grounder.py:575,582,...`). A researcher who wants a quick shallow scan (3 per source) vs. a deep one (20 per source) can't choose.

**Fix:** add `limit_per_source` to `CreateRunRequest` (default 8, range 1–25). Thread it through `pipeline.advance` → agent `run()` kwargs. This is a one-line change per agent signature plus a UI number input on the New Run screen.

## U6 — Break 0 has no "add a theme" widget

**Severity: Medium · Effort: S**

`app.js:1039–1041` references `draft.addedThemes` and `buildDirectives` (`app.js:1263`) emits `ADD THEME: <id>` — but there is no UI widget that populates `addedThemes`. The checkbox only toggles `removedThemes` for themes the concept mapper already activated. A researcher who knows their problem touches a theme the mapper missed (e.g. adding `philosophy_of_technology` to a problem about AI) has no way to add it from the UI — they have to use the free-text box and type `ADD THEME: philosophy_of_technology` manually, which requires knowing the exact `theme_id` from `config.json`.

**Fix:** add an "Add theme" input below the theme grid that autocompletes from the full `config.themes` list (fetchable via a new `GET /api/themes` endpoint or included in the break payload). Selecting a theme adds it to `addedThemes` and renders it as a new chip in the grid.

## U7 — No estimated time remaining or per-step timing history

**Severity: Low · Effort: S**

The live card shows elapsed time for the current step (`app.js:629`) but not:
- Estimated remaining time (average step duration × steps remaining).
- Per-step historical duration (useful to spot "Grounder took 25 min last time, this time it's at 40 min — something's wrong").
- Per-source time within a step.

**Fix:** the `run_steps` table already records `started_at` and `finished_at`. Compute average per step from past runs and show "≈ 12 min remaining" in the live card. Show a duration on each rail step on hover (already shows `error`/`label` in `title` — add duration).

## U8 — No per-source progress within a step

**Severity: Medium · Effort: M**

When Grounder is running, the live card shows "Grounder — searching OpenAlex: <query>" and that's it. A 5-theme, 8-source Grounder run makes ~40 source calls and the user has no idea where in that sequence it is. The activity log helps but it's append-only and you have to scroll.

**Fix:** the limiter already tracks `_call_counts` per source (`rate_limiter.py:72`). Expose a `progress` field on the `run_steps` row: `{source: "openalex", calls: 3, total: 5}`. The agent updates it via `progress.note` (extend `note` to accept a structured `progress` arg). The UI live card renders a sub-progress bar under the activity line.

## U9 — No way to pause a single source mid-step

**Severity: Low · Effort: M**

The only stop control is whole-run stop (`/api/runs/{id}/stop`). If a researcher sees Grounder is spending 10 minutes hammering a dead Semantic Scholar, they can't tell it "skip Semantic Scholar, keep going with the others" — they have to stop the whole run, disable Semantic Scholar in `config.json`, and resume.

**Fix:** subsumed by U1 (per-run source overrides) + the circuit breaker (E3). With both, the user can stop the run, uncheck Semantic Scholar in the resume panel, and resume — the step restarts without that source.

## U10 — No config export/import

**Severity: Low · Effort: S**

A researcher who has tuned a run's model routing, source selection, and token budgets has no way to save that configuration and apply it to the next run. Every new run starts from `config.json` defaults.

**Fix:** add `GET /api/runs/{id}/config` returning the effective config for that run (model overrides + source overrides + per-agent settings). Add a "Copy settings from previous run" picker on the New Run screen that pre-fills the form from a past run.

## U11 — No dark/light theme toggle (follows system only)

**Severity: Low · Effort: S**

`README.md:256` claims the UI "follows your system light/dark preference." `style.css` presumably uses `prefers-color-scheme`. A user who wants dark mode on a light-default system (or vice versa) has no toggle. Minor, but it's the kind of thing that reads as "no config control" to a new user.

**Fix:** add a theme toggle in the topbar that sets a `data-theme` attribute on `<html>`, with localStorage persistence. CSS uses `[data-theme="dark"]` selectors that override the `@media (prefers-color-scheme: dark)` rules.
