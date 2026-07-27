# 3 — Research efficacy: is the output worth what it costs?

The README's promise is specific and testable: *"every claim traces to verifiable evidence"* and *"It maps the intellectual territory so you can navigate it yourself."* This section tests that promise against the code and against the 7,076 sources in the shipped database.

The short version: the pipeline gathers evidence well and ranks it well, then discards most of the ranking and a sixth of the evidence at the two points where it matters most — the link check and the context boundary. Neither loss is visible to the researcher.

---

## V1 — Context truncation ignores the rankings the pipeline paid to compute

**Severity: Critical · Effort: M**

`core/context.py` assembles what each agent sees. Every list is truncated by a bare slice of an unordered query:

```python
for s in seminal[:25]:                                            # line 267
for g in gaps[:15]:                                               # line 291
for i in implications[:12]:                                       # line 300
for s in sources[:max_items]:                                     # line 26  (default 20)
for p in (viable + infeasible)[:8]:                               # line 319
```

The sources come from `db.get_sources_by_type` → `db.fetch`, which has no `ORDER BY` support at all (`core/database.py:429-445`). So "the 15 gaps Scribe sees" means *whichever 15 rows the storage engine returns first*.

This throws away work the pipeline already did:

| Column | Set by | Used at the truncation? |
|---|---|---|
| `sources.relevance_rating` (High/Medium/Low) | one LLM call per source | **No** |
| `gaps.significance` (High/Medium/Low) | Gaper | **No** |
| `implications.strength` (Strong/Moderate/Speculative) | Vision | **No** |
| `proposals.promise_rating` | Theorist | **No** |

The shipped database shows what that costs. Across 6,510 rated `current` sources:

```
Low:    5845   (89.8%)
Medium:  581   ( 8.9%)
High:     84   ( 1.3%)
```

An unordered slice of 20 from that population yields, on average, **18 Low, 2 Medium, 0.3 High**. The Understanding Map — the deliverable — is written from a near-random draw dominated by material the pipeline itself judged irrelevant. In the same database, `gaps` holds 356 rows against a `[:15]` cap: 96% of the gap analysis never reaches Vision, Theorist or Scribe, and which 4% survives is arbitrary.

Notably, `agents/gaper.py:270` does this correctly:

```sql
ORDER BY CASE relevance_rating WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, year DESC
```

The pattern is known. The context builder just does not use it.

**Fix.**

1. Add `order_by` to `db.fetch` and give every getter a sensible default: sources by relevance then year, gaps by significance, implications by strength, proposals by promise.
2. Make truncation deliberate: select top-N *by rank*, and where a category has more, include a one-line tail summary (`"+ 341 further gaps: 12 High, 88 Medium, 241 Low — see the Gaps tab"`) so the agent knows the list is partial and the researcher can go find the rest.
3. Record the selection in the artifact footer: which items were included, which were dropped, on what ranking (see V6).

This is the highest-value change in the review. It is a day's work and it improves every artifact the system produces.

---

## V2 — The liveness check has silently discarded 17.2% of every source ever collected

**Severity: High · Effort: M**

Every result gets a `HEAD` request before it can enter the run (`agents/social.py:91-107`):

```python
try:
    resp = requests.head(url, timeout=10, allow_redirects=True, ...)
    ...
except Exception:
    return "dead"
```

Any exception — a 10-second timeout, a TLS handshake failure, a DNS blip, a connection reset, a publisher that rejects `HEAD` at the transport layer — is recorded as `dead`. And `dead` is fatal (`agents/social.py:1146-1159`): the source is written to `dead_links` and `continue`d past, never entering `sources`.

In the shipped database:

```
sources kept:         7076
dead_links archived:  1475      →  17.2% discarded
```

One in six retrieved sources was thrown away on the strength of a single un-retried `HEAD` request. The rate limiter's backoff, retry and circuit breaker — all carefully built for `_get` — do not apply to `_check_link` at all. A thirty-second network wobble during a Social step permanently removes every source in flight from the run, and the Understanding Map is written as though those papers do not exist.

The judgement is also wrong on the merits. A paper with a temporarily unreachable landing page is still a real paper: it has a DOI, a title, authors and an abstract already in hand. Liveness of a URL is metadata about a link, not about a source's evidentiary value.

There is a second-order inconsistency: 566 rows in `sources` carry `link_status = 'dead'` (written by Grounder/Historian paths and by `recheck_links`), so "dead means dropped" is not even applied uniformly.

**Fix.**

1. Never drop a source for a failed link check. Store `link_status` and let downstream agents and the UI weigh it. A source with a DOI always has `https://doi.org/{doi}` as a durable fallback.
2. Distinguish `unreachable` (exception/timeout) from `dead` (a definite 404). Only the latter is evidence about the source.
3. Route `_check_link` through the rate limiter and give it one retry, as `_get` has.
4. Make the check optional and off by default (`config.sources._defaults.verify_links`). It costs one HTTP round-trip per result — thousands per run — for information that is not used in any ranking.

---

## V3 — One LLM call per source produces a rating nothing downstream filters on

**Severity: High · Effort: M**

`rate_relevance` is called once per retrieved result (`agents/social.py:1121`). With ~10 sources × 8 results × N themes, a normal run makes several hundred to a few thousand model calls purely for rating — comfortably the dominant LLM cost of the pipeline.

Grep for consumers of the resulting column:

```
agents/gaper.py:199    counts High-rated sources for a summary line
agents/gaper.py:270    orders one query by rating
agents/social.py:1387  groups by rating when writing its own doc
```

That is all. No context builder filters on it, no artifact reports it, and no agent other than Gaper orders by it. The most expensive signal the pipeline produces is used for one `ORDER BY` and one count.

Combined with C2 (these very calls lose the run binding and may all be failing back to `"Medium"`), a large fraction of the run's model spend may be producing a constant.

**Fix.**

1. Batch. One call rating 20 papers against a problem returns better-calibrated *relative* judgements than 20 independent calls, and cuts the call count by 20×. Send `[{id, title, abstract[:300]}]`, expect `[{id, rating, reason}]`.
2. Use the `light` model role for rating. It is a triage judgement; it does not need the primary model. `config.json` already supports per-agent roles, and there is no reason Social's rating path should route the same way as its synthesis path.
3. Then actually spend the signal: filter and order by it at the context boundary (V1), show the distribution in the UI (X3), and let the researcher set a floor ("ignore Low") per run.

---

## V4 — No token budgeting: context is sliced by characters, not tokens

**Severity: Medium · Effort: M**

Truncation is by character count (`abstract[:400]`, `narrative[:1200]`) and item count. Nothing measures the assembled prompt against the target model's context window. `for_understanding_map` can concatenate a full argument tree, 25 abstracts at 400 chars, 20 historical entries, 15 gaps, 12 implications, 8 proposals with reasons, and a 1,200-char narrative — comfortably past 32k tokens for a local model, and the pipeline's default provider is a *local* Open-WebUI endpoint.

When it overflows, the provider returns an error, `_attempt` walks the fallback chain, and if no rung succeeds the step fails at the very last stage — after every expensive step has already run. The researcher's twenty-minute run ends with `LLMError` on Scribe.

**Fix.** Add a rough token estimator (chars/4 is adequate) and a per-agent `context_budget_tokens` in `config.json`. Fill the budget by rank (V1 gives you the ordering), and record what was dropped. When a section must be cut hard, summarise it rather than truncating mid-item — a half-sentence abstract is worse than no abstract.

---

## V5 — A router failure is recorded as a real "Medium" rating

**Severity: Medium · Effort: S**

```python
except Exception as e:
    logger.warning(f"Relevance rating failed: {e}")
return "Medium", "Could not assess relevance"
```
(`agents/social.py:922-924`)

The fallback is written into `sources.relevance_rating` as if it were a judgement. Nothing distinguishes "the model considered this and said Medium" from "the model was unreachable". Given C2, this is not hypothetical: in a multi-user deployment without a global fallback provider, *every* rating could be this string, and the run would still report success. The shipped database happens to have zero of these rows, which is consistent with them having been produced by the CLI path where the binding is intact — the web path is the one at risk.

**Fix.** Write `relevance_rating = NULL` and `relevance_reason = "not assessed: <reason>"` on failure, count the failures, and raise them through `progress.warn()` so they appear on the step's warning badge — the mechanism already exists (`core/pipeline.py:208`). A run where 80% of sources are unrated is a run whose Understanding Map should carry a caveat.

---

## V6 — No evidence-coverage report on the final deliverable

**Severity: Medium · Effort: M**

`core/references.py` is a real asset — APA formatting, cite-key assignment, a coverage matrix, `find_unknown_cite_keys` for hallucinated citations, and online DOI verification against Crossref with a cache table. Most projects in this space have nothing like it.

But the researcher receiving an Understanding Map cannot see any of it. The artifact does not state:

- how many sources were retrieved, how many survived the link check, how many reached the writing agent;
- which sources are cited versus merely collected;
- which cite keys failed verification, or whether verification ran at all;
- which themes came back empty, and which sources were skipped for a missing key or a tripped breaker — `source_health` records exactly this and it stays in the API;
- what was truncated (V1) and what was discarded (V2).

Without that, "every claim traces to verifiable evidence" is a claim the reader has to take on faith. The data to substantiate it is already in the database.

**Fix.** Append a "Provenance" section to every Scribe artifact, generated from `source_health`, `reference_verifications`, `llm_usage` and the truncation decisions:

```
Evidence base: 412 retrieved · 71 discarded (unreachable) · 341 retained
               58 rated High · 96 Medium · 187 Low
Cited:         34 sources · 31 DOI-verified · 3 unverified (listed below)
Coverage:      9 of 11 themes returned results
               scopus  — skipped, no API key
               core    — circuit breaker tripped after 5 failures
Context:       Scribe saw the top 25 of 341 sources and 15 of 356 gaps, ranked by
               relevance and significance.
```

That paragraph converts the output from "a document an AI wrote" into "a document with a known evidence base and known limits" — which is exactly the difference the README says the project exists to create.
</content>
