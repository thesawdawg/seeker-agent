# 6 — Engineering hygiene

---

## H1 — No CI: a green 209-test suite that nothing runs

**Severity: High · Effort: S**

There is no `.github/` directory and no CI configuration of any kind. The suite is substantial and it passes:

```
209 passed, 14 skipped, 1 warning in 84.18s
```

11 test modules cover the LLM router, the web API, cancellation, pipeline state, source controls, MySQL dialect handling, directive round-tripping, break instructions, unattended source degradation and frontend asset integrity — plus five subprocess scenario scripts. `test_step_artifacts_path_traversal_blocked` and `test_artifact_read_is_confined_to_the_artifacts_directory` show security regressions were deliberately pinned down.

None of it is enforced. Every finding in `01-correctness-and-reliability.md` reached `main` through a repository whose only gate is the author remembering to run `pytest`. C1 and C2 in particular are the kind of defect a matrix build catches immediately once the regression test exists.

**Fix.** A workflow running `pytest` on push and PR, on 3.11 and 3.12 (which will surface C8), plus a MySQL service container so `test_mysql_backend.py` stops being skipped — the MySQL dialect is the deployment path, and it is the one currently untested in practice.

---

## H2 — Test dependencies are undeclared, and the suite fails misleadingly without them

**Severity: Medium · Effort: S**

`requirements.txt` omits `pytest` and `httpx`; they live only in `pyproject.toml`'s `dev` extra, which nothing in the README or `CONTRIBUTING.md` tells a contributor to install. `mcp` is in `requirements.txt` but commented as optional in `pyproject.toml`.

The failure mode is actively misleading. On a machine where `cryptography` is present but its `_cffi_backend` is broken, and `mcp` is absent, the first run of the suite produced:

```
79 failed, 130 passed, 14 skipped, 69 errors
```

with errors reading `RuntimeError: This portal is not running` — a Starlette TestClient artefact that says nothing about the real cause. A new contributor would reasonably conclude the project is broken. After installing working `cryptography` and `mcp`, the same tree gives 209 passed.

**Fix.** Add a `[dependency-groups] test = [...]` (or requirements-dev.txt), document `pip install -e '.[dev,web,mysql]'` in CONTRIBUTING, and make the suite fail fast with a clear message when a hard dependency is missing rather than degrading into 69 opaque errors.

---

## H3 — Real run outputs, logs and exports are committed

**Severity: Medium · Effort: S**

`.gitignore` lists `artifacts/*`, `logs/*` and `exports/`, but the following are tracked, because ignore rules do not untrack:

```
artifacts/RUN-20260331-152508-D296_*.md      13 files — a complete run's output
artifacts/RUN-20260407-022355-242D_understanding_map.md
logs/RUN-20260401-152657-4099.log
logs/RUN-20260401-152739-33BC.log
exports/seminal_references.csv               93 KB
exports/seminal_references.json              569 KB
db/pipeline.db                               16 MB   (see S6)
db/consensus_tokens.json
```

No credentials appear in the logs (checked). The cost is repository weight — every clone pulls 16 MB of somebody else's SQLite database — and a permanently dirty working tree, since the app writes to tracked paths during normal use.

**Fix.** `git rm --cached` all of the above. Keep the `.gitkeep` files. If sample output is useful for documentation, curate one small artifact under `docs/examples/` and commit it deliberately.

---

## H4 — Dependencies are floor-pinned only, with no lockfile

**Severity: Medium · Effort: S**

Every requirement is `>=`:

```
requests>=2.31.0
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
cryptography>=42.0.0
```

`Dockerfile:19` runs `pip install --no-cache-dir -r requirements.txt`, so two builds of the same commit a month apart can install materially different FastAPI or Starlette versions. There is no lockfile and no hash pinning. The deprecation warning already visible in the test run —

```
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated
```

— is exactly the kind of thing that becomes a build failure on an unrelated day.

**Fix.** Generate a lockfile (`uv lock`, `pip-compile`, or `poetry.lock`) and install from it in the Dockerfile. Keep the loose ranges in `pyproject.toml` for library consumers.

---

## H5 — Dead code and stale comments after the M-series refactor

**Severity: Low · Effort: S**

Small things, all cheap:

- `core/pipeline.py:718` — `executed = 0` in `advance()` is assigned and never used; the real counter lives in `_advance_loop`.
- `agents/social.py:1106` — *"The LLM router holds no per-call state (review O3)"* is the comment that licences C2. It should say the router holds per-**run** state and that the context must be propagated.
- `core/database.py:1119` — *"the UNIQUE constraint makes this safe"* is wrong (C6) and is the kind of comment that stops the next reader from looking.
- `core/pipeline.py:1153` — a three-line comment explains that source-ID remapping is "handled below in a second pass for simplicity", immediately above code that does nothing with it. True, but the placement reads as an unfinished edit.
- `core/llm.py:183` — `_warned_missing_model` is annotated `set` but holds `(provider, role)` tuples; the type hint should say so.
- `agents/social.py:1113` — `from concurrent.futures import ThreadPoolExecutor` re-imported inside a loop body that already imported it at line 1069.
- `core/database.py:389` — `backend._settings["database"]` reaches into `MySQLBackend`'s private attribute from outside the module; add a `target` property to `Backend`.

---

## What is worth protecting

Listing these because a review that only enumerates defects gives a false impression of the codebase, and because these are the parts a refactor should be careful not to damage:

- **`core/llm.py`** — a genuinely provider-agnostic seam. Two wire formats, a fallback ladder, per-agent profiles, per-run overrides, and `describe_plan()` so the UI and CLI show the same routing the agent will get. The retry/backoff logic is correct.
- **The step state machine.** Modelling the pipeline as rows with explicit status is what makes three drivers (CLI, worker, web) share one implementation, and what makes a twenty-minute run survive a web restart. `NON_FATAL`, `downstream_steps` and `_purge_outputs` are the right primitives.
- **Cooperative cancellation with a hard deadline.** `progress.note()` doubling as the checkpoint is a genuinely clever move — the cancellation point is placed exactly where the slow call is about to happen, with no plumbing at the call site. The `STOP_GRACE_SECONDS` escape hatch, enforceable from the web process so a wedged worker cannot hold a run hostage, shows the failure mode was thought through.
- **`db_backend.py`'s type vocabulary.** Writing the schema once in `{ID}`/`{KEY}`/`{LONGTEXT}` tokens and rendering per dialect is a clean answer to the SQLite/MySQL split, and `ensure_columns` correctly handles the "CREATE TABLE IF NOT EXISTS silently ignores new columns" trap that bites most projects.
- **The markdown renderer.** Escape-then-substitute with a scheme allowlist on links (`web/static/app.js:78-107`). Hand-rolled sanitisers are usually a finding; this one is correct.
- **`core/references.py`.** Cite-key assignment, a coverage matrix, hallucinated-citation detection and cached Crossref verification. Underused (V6) but well built.
- **The break screens.** Showing the exact directive text being submitted, so the interface stays honest about what it is doing on the user's behalf, is the single best design decision in the UI.
</content>
