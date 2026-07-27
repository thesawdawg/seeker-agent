# 5 — UX and the information the interface owes the user

The UI is better than the codebase's age would suggest: no build step, no framework, no external assets, and a genuinely careful break-screen design where the exact directive text being submitted is always shown. The step rail with per-step activity, elapsed time and a warning badge is the right answer to "a twenty-minute run should not be an opaque spinner". The login copy is clear about what the key is for and that it is stored encrypted.

What follows is what the interface does not tell the user, and what it tells them that is not true.

---

## X1 — The pre-flight tells users their stored source keys will be used; they will not

**Severity: High · Effort: S**

`GET /api/sources/health` (`web/app.py:625`) is the New Run screen's pre-flight, and it reports per source:

```python
stored = bool(users.get_source_api_key(user["user_id"], source_id))
has_key = env_set or stored
note = "env" if env_set else ("stored" if stored else "no key — will skip")
```

The user sees Scopus marked ready with `stored`. Per **C1**, the worker never sees that key: the contextvar binding is lost on the thread hop, `keys.get()` falls through to `os.environ`, and Scopus is queried without a key — or skipped.

This is worse than a missing feature. The researcher makes a go/no-go decision on the strength of a coverage claim the system cannot honour, and the run's Understanding Map is written from a narrower evidence base than they were told to expect.

**Fix.** Fix C1. Until then the endpoint should not report `stored` as a satisfied key. Long term, verify rather than assert: the pre-flight should make one cheap authenticated call per keyed source and report what actually came back.

---

## X2 — No cost or duration estimate before committing to a run

**Severity: High · Effort: M**

A run is a long, expensive, human-blocking commitment: three mandatory breaks, minutes-to-hours of wall clock, and thousands of LLM calls. The New Run screen asks for a problem statement and a model, and starts.

Nothing before the "Start" button says: how many themes were selected, how many sources will be queried, roughly how many model calls that implies, or roughly what it will cost. Everything needed is available — `/api/steps` knows the shape and the per-step service list, `config.json` has `limit_per_source`, and `llm_usage` has real per-agent token history from previous runs.

The token panel exists (`web/static/app.js:1560`) but only *after* the fact, under the Overview tab of a finished run.

**Fix.** A pre-flight summary card:

```
11 themes × 9 sources × 8 results   ≈ 790 source lookups
Estimated model calls ≈ 890  ·  ~1.2M tokens
Based on your last 3 runs: ~34 minutes of compute, 3 breaks awaiting you
Scopus will be skipped — no key
```

Derive the estimate from this user's own `llm_usage` history and fall back to a static table for a first run. Show a live running total against the estimate while the run executes.

---

## X3 — Truncation and discard are invisible

**Severity: Medium · Effort: M**

V1 and V2 both cause silent data loss, and neither surfaces anywhere in the interface:

- 17.2% of retrieved sources were dropped by the link check. The Sources tab shows `inserted` counts from `source_health`, so a researcher sees "OpenAlex: 41" with no way to know 9 more were found and discarded.
- Scribe saw 25 of 341 sources and 15 of 356 gaps. Nothing says so, in the UI or in the artifact.

The mechanism to say it already exists: `record_step_warning` (`core/pipeline.py:208`) persists per-step warnings, and the rail already renders a badge with a tooltip (`web/static/app.js:1041-1052`). Nothing calls it for truncation or discard.

**Fix.** Emit `progress.warn()` when a context section is truncated and when sources are dropped by liveness. Add a "Coverage" strip to the run Overview:

```
Retrieved 412 → discarded 71 (unreachable) → retained 341
Scribe saw the top 25 by relevance · 316 not shown
```

Make the discarded set inspectable — `dead_links` already stores every one of them with its title and original URL, and a researcher who recognises a paper in that list has learned something important about their run.

---

## X4 — Dynamically built controls have no labels, and status changes are not announced

**Severity: Medium · Effort: M**

`web/static/index.html` contains 7 `<label>` elements and 2 ARIA attributes across 659 lines. Everything built at runtime by `el()` — the source checkbox grid (`buildSourceGrid`), the per-agent model selects (`buildModelGrid`), the role grid, the break widgets, the tab strip — is constructed without labels, without `aria-label`, and without programmatic association between control and text.

The two ARIA attributes present are on the brand button and the modal (`role="dialog" aria-modal="true"`).

The specific consequences:

- **The tab strip** is a set of `div.tab` elements with click handlers and an `is-active` class. Not reachable by keyboard, not announced as tabs. It should be `role="tablist"` with `role="tab"` buttons, `aria-selected`, and arrow-key handling.
- **`#toasts`** has no `aria-live`, so every error and confirmation — including "Your session expired" — is silent to a screen reader.
- **The live activity line and step rail** update continuously with no `aria-live="polite"`, so the one thing a blind user most needs during a twenty-minute run (progress) is unavailable.
- **The source checkbox grid** relies on adjacent text nodes rather than `<label for>`, so the checkbox announces as unlabelled and has a tiny hit target.

**Fix.** Give `el()` first-class label support and use it everywhere a control is generated. Add `aria-live="polite"` to `#toasts`, the activity log and the rail's current-step line. Convert the tab strip to a proper tablist. This is mechanical work with a clear finish line.

---

## X5 — Nothing warns that changing your provider key loses your account

**Severity: Medium · Effort: S**

Per S3, identity is the fingerprint of the provider API key. Rotate the key and you become a different user with no access to your own runs. The login screen says:

> Sign in with the API key for your own model provider. There is no separate password — your provider key identifies you.

That is honest about the mechanism and silent about the consequence. A user who rotates a leaked OpenAI key — the correct thing to do — will find their research history gone and no explanation anywhere in the product.

**Fix.** State it at the point of decision, on the login screen and in the credentials panel:

> Your provider key **is** your account. If you rotate or replace this key, you will sign in as a new user and previous runs will not be visible. Add a recovery identity in Settings first.

And then build the recovery path (S3), because a warning about unrecoverable data loss is only acceptable if there is something the user can do about it.

---

## X6 — The run list is unpaginated, unfiltered and unsearchable

**Severity: Low · Effort: S**

`GET /api/runs` returns every run the user owns, sorted in Python (`web/app.py:296-319`), and `showRuns` renders all of them. At 34 runs — the count in the shipped database after a few months — that is already a wall of pills with no way to search by problem text, filter by status, or find "the run that was waiting on Break 1".

Runs are the primary object in a tool designed for longitudinal research. There is also no delete or archive: nothing in the API removes a run, so the list only ever grows, and with it the `sources`/`gaps`/`artifacts` tables.

**Fix.** Server-side `?status=&q=&limit=&offset=` (which O2's query rewrite gives you for free), a text filter in the UI, and archive/delete endpoints with the cascading purge that `_purge_outputs` already knows how to do.

---

## X7 — No reduced-motion support; focus styling only on form fields

**Severity: Low · Effort: S**

`style.css` handles `prefers-color-scheme: dark` properly (line 23) but has no `prefers-reduced-motion` block, and the only focus rule is `input:focus, select:focus, textarea:focus` (line 149). Buttons, tabs, the brand nav element and the rail's per-step controls get whatever the reset leaves them, which on a custom-styled button is usually nothing visible.

**Fix.** Add a `@media (prefers-reduced-motion: reduce)` block disabling transitions and the spinner animation, and replace `:focus` with a `:focus-visible` rule applied to every interactive element, buttons included.

---

## Smaller notes

- **`GET /api/runs/{id}/status` mutates.** It calls `pipeline.enforce_stop_deadline` (`web/app.py:374`), so a GET can force-stop a run and discard a step. The reasoning is sound — the deadline must land even if the worker is wedged — but a side-effecting GET is retried by proxies and prefetched by browsers. Move it to the SSE stream and a periodic worker sweep.
- **`DELETE /api/blacklist` takes a request body** (`web/app.py:751`). Legal but poorly supported; several proxies and HTTP clients strip bodies from DELETE. Use query parameters or `POST /api/blacklist/remove`.
- **Break submission is one-shot.** `submit_break` requires `awaiting_break == break_num` and returns 409 otherwise (`web/app.py:1015`), telling the user to "re-run the break step". Re-running a break cascades a reset over everything downstream — a harsh outcome for a typo in a directive. Allow editing a submitted break's instructions while the following step has not yet started.
- **No confirmation of what a break directive will do.** The preview text is shown, which is excellent, but there is no dry-run: "REMOVE GAP GAP-3" does not tell you which gap that is or what depends on it. `rerun_impact` already models this idea for steps; breaks deserve the same.
</content>
