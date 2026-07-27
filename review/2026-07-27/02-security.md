# 2 — Security

Threat model assumed: the Docker Compose deployment described in the README, reachable by more than one person. Single-user localhost use makes S1, S2 and S5 much less interesting; everything else still applies.

Worth stating up front, because it is unusual: several things here are done *right*. Provider keys are encrypted at rest and refuse to downgrade to plaintext (`core/crypto.py:34-43`). Artifact reads are confined by `resolve().relative_to(root)` on both endpoints (`web/app.py:1184`, `1261`). `require_run_access` returns 404 rather than 403 so run existence is not disclosed (`web/auth.py:238`). The session cookie is `HttpOnly` + `SameSite=Lax` (`web/app.py:176`), which closes ordinary CSRF on the state-changing routes. The markdown renderer escapes before substituting and restricts link schemes to `http/https/mailto` (`web/static/app.js:78-107`) — the obvious stored-XSS path from LLM output and external paper titles is genuinely closed.

---

## S1 — Unauthenticated SSRF via `POST /api/auth/login`

**Severity: High · Effort: M**

`LoginRequest.base_url` is a bare `str` (`web/app.py:76`). `auth.login` passes it straight to `validate_provider_key`, which fetches it (`web/auth.py:129`):

```python
resp = requests.get(f"{base_url}/models", headers=headers, timeout=15)
```

There is no scheme check, no host allowlist, no private-range block, and **no authentication** — `/api/auth/login` is by definition open. Anyone who can reach the page can make the server issue arbitrary GETs from inside the deployment's network.

The responses are usefully distinguishable, which turns it into a working scanner:

| Outcome | Response |
|---|---|
| Connection refused / no route | `502` with the exception text and the URL |
| Reachable, returns 401/403 | `401 "The provider rejected that API key"` |
| Reachable, other 4xx/5xx | `502 "Provider returned HTTP {code}"` |
| Reachable, 200, not JSON | `502 "Provider did not return JSON"` |
| Reachable, 200, JSON | `200` plus every `id`/`name` in the payload |

That last row is exfiltration, not just probing: any internal JSON endpoint whose payload contains `id` or `name` keys has those values returned to the caller. Cloud metadata services, internal admin APIs and the MySQL/Redis containers on the compose network are all in reach.

It does not stop at login. `PUT /api/credentials` stores the URL, and the worker then POSTs prompts to `{base_url}/chat/completions` (`core/llm.py:222`) with the user's key in an `Authorization` header — an authenticated outbound request to an attacker-chosen host, repeated for the life of the run.

**Fix.** Validate at the Pydantic layer and enforce at the request layer:

- Require `http`/`https`, reject credentials-in-URL, reject non-standard ports unless allowlisted.
- Resolve the hostname and refuse loopback, link-local (`169.254.0.0/16`), and RFC1918 ranges — unless `SEEKER_ALLOW_PRIVATE_PROVIDERS=1`, which local Ollama/LM Studio users need and should set deliberately.
- Re-check after DNS resolution and pin the resolved IP for the request, so a DNS-rebind between validation and use does not slip through.
- Collapse the error responses to one opaque message; keep the detail in the server log.
- Optional and strongest: `SEEKER_PROVIDER_ALLOWLIST` of host patterns, empty meaning "anything public".

---

## S2 — "First user" admin promotion selects an arbitrary row

**Severity: High · Effort: S**

`core/users.py:101-112`:

```python
all_users = db.fetch("users", {})
if any(u.get("is_admin") for u in all_users):
    return
first = all_users[0]
db.update("users", {"is_admin": 1}, {"user_id": first["user_id"]})
logger.info(f"[F12] Auto-promoted first user {first['user_id']} to admin")
```

`db.fetch` emits `SELECT * FROM users` with no `ORDER BY` (`core/database.py:432`). On MySQL InnoDB the rows come back in primary-key order, and the primary key is `generate_id("USR")` = `USR-` + 8 random hex characters (`core/utils.py:38`). The row promoted is therefore **random**, not the first registered — while the log line asserts otherwise.

`init_users_tables()` re-runs this on every process start, so if the admin's account is ever deleted, the next boot silently hands admin to another arbitrary user. Admin means write access to `config.json` sections including `sources` and `agent_sources`.

**Fix.** `ORDER BY created_at ASC LIMIT 1`, and gate the whole auto-promotion behind an explicit opt-in (`SEEKER_AUTO_PROMOTE_FIRST_USER=1`, default off) so a multi-user deployment never grants admin implicitly. `SEEKER_ADMIN_USER_ID` already covers the deliberate case.

---

## S3 — The provider key is the password, and rotating it destroys the account

**Severity: Medium · Effort: M**

Identity is `crypto.fingerprint(api_key)` (`web/auth.py:167`). Three consequences follow:

1. **The credential is a bearer token, not a password.** Provider API keys get pasted into scripts, CI, `.env` files and shell history. Anyone who obtains one gets the SEEKER account attached to it — every run, every artifact, and the stored copies of the user's other provider credentials.
2. **Rotation is account loss.** Rotate your OpenAI key — exactly what you should do after a leak — and the new key fingerprints differently, so `get_or_create` makes you a *new user*. Your runs, owned by the old `user_id`, become invisible (`require_run_access` → 404) with no recovery path.
3. **There is no revocation.** No "sign out everywhere", no session list. A leaked session token is valid for `SEEKER_SESSION_TTL` (default 24h) and cannot be invalidated except by restarting the web process on the in-memory store, or by hand in Redis.

**Fix.** Decouple identity from the credential. Keep key-based login as the bootstrap, but on first login mint a stable account identifier and let a signed-in user attach or replace provider credentials without changing who they are. Add an explicit "add a recovery identity" step. At minimum, warn in the UI before a user's first run that their provider key *is* their account (see X5).

---

## S4 — `SEEKER_SECRET_KEY` rotation orphans every user, with no migration path

**Severity: Medium · Effort: S**

`crypto.fingerprint` is keyed by `SEEKER_SECRET_KEY` (`core/crypto.py:105`) and `crypto.encrypt` derives its Fernet key from the same value. Rotating it — the correct response to a suspected server compromise — does two things at once: every user's `auth_ref` changes so nobody can log back into their own account, and every stored `api_key_enc` becomes undecryptable, so `provider_config` raises `SecretUnavailable` and every queued run fails at the worker.

`decrypt` produces a good diagnostic (*"Has SEEKER_SECRET_KEY changed since it was saved?"*), which means the failure mode was anticipated but not solved.

**Fix.** Support `SEEKER_SECRET_KEY_PREVIOUS` for decrypt-and-re-encrypt on next use, and store a non-secret random per-user salt as the fingerprint input instead of keying the HMAC with the server secret — identity then survives rotation while remaining non-reversible. Document the rotation procedure either way; today rotating is a data-loss event that nothing warns about.

---

## S5 — Unauthenticated endpoints leak deployment configuration

**Severity: Medium · Effort: S**

Three routes have no `Depends(auth.resolve_user)`:

| Route | Discloses |
|---|---|
| `GET /api/health` (`web/app.py:155`) | Storage backend, live queue depth, whether secrets are configured |
| `GET /api/agents` (`web/app.py:279`) | Every agent name with its model role, `max_tokens`, `temperature` |
| `GET /api/steps` (`web/app.py:1041`) | Full pipeline shape and every source each step is configured to call |

None is catastrophic. Together they let an anonymous visitor fingerprint the deployment, confirm encryption is *not* configured (`secrets_configured: false` — a direct signal that credential storage is refused and something is misconfigured), and watch queue depth as a liveness/load oracle.

**Fix.** Put `/api/agents` and `/api/steps` behind `resolve_user` — the UI only calls them after sign-in. Reduce `/api/health` to `{"status": "ok"}` for anonymous callers, since that is all the Docker healthcheck needs (`Dockerfile:32`), and keep the detail for authenticated ones.

---

## S6 — A 16 MB database of 34 real runs is committed to the repository

**Severity: Medium · Effort: S**

`db/pipeline.db` is listed in `.gitignore` but is *tracked* — `.gitignore` never untracks a file already committed. `git ls-files db/` confirms it, along with `db/consensus_tokens.json`.

Contents:

```
runs 34 · sources 7076 · dead_links 1475 · gaps 356 · implications 487
proposals 144 · evaluations 176 · syntheses 19 · directions 152 · artifacts 19
```

These are real research problems and real generated conclusions from the maintainer's own use, published to everyone who clones. There is no `users` table in it, so no credentials leaked — but the 34 `problem` strings are the researcher's actual questions. `artifacts/`, `logs/` and `exports/` are tracked the same way despite matching ignore patterns (see H3).

`db/consensus_tokens.json` currently holds only OAuth *client registration* (`client_id`, `token_endpoint_auth_method: "none"` — a public client, no secret). No access or refresh token is present today, but the path is one successful OAuth flow away from committing live tokens, and the file is already tracked so it will be picked up automatically.

Secondary effect: because the file is tracked and the app writes to it, every local run dirties the working tree. Merely opening it read-only during this review produced ` M db/pipeline.db`.

**Fix.** `git rm --cached db/pipeline.db db/consensus_tokens.json` and the artifact/log/export files. If the sample data has demo value, ship a small curated fixture under `tests/fixtures/` instead. Consider whether the history needs rewriting — the runs are already public in every prior commit.

---

## S7 — No login throttling; the preview endpoint is an unmetered outbound proxy

**Severity: Low · Effort: S**

`POST /api/auth/login` has no rate limit, no lockout and no backoff, so it doubles as the engine for S1's scanning and as a free way to make the server hammer a third-party endpoint.

`POST /api/runs/{id}/break/0/preview` (`web/app.py:965`) is authenticated but unmetered: each call fires live OpenAlex and Semantic Scholar queries. A signed-in user can loop it to burn the deployment's shared quota — the rate limiter throttles but does not cap, and `source_call_log` only enforces daily limits for sources that declare one (OpenAlex alone, in `SOURCE_LIMITS`).

**Fix.** A small per-IP limiter on `/api/auth/login` (10/minute is generous) and a per-user hourly cap on preview.

---

## S8 — The admin config editor has no size limit, audit trail, or write lock

**Severity: Low · Effort: S**

`PUT /api/config/sections/{section}` (`web/app.py:801`) accepts an unbounded `dict` body and merges it into `config.json`. Observations:

- No size limit. A large body becomes a large `config.json`, which `load_config()` then parses on every request (see O4).
- Concurrent admins race: both read, both merge, last write wins silently — the atomic rename protects against a *torn* file, not against a lost update.
- The only record is `logger.info(f"[F12] Admin {user['user_id']} updated section '{section}'")` — no before/after, no way to answer "who broke the source list and when".
- `sources` is editable and its values feed the source handlers; combined with a wrongly-assigned admin (S2) that is a configuration-tampering path.

**Fix.** Cap the body (256 KB is ample), take a timestamped backup copy before each write, and record `{user_id, section, timestamp, diff}` to a `config_audit` table. An `If-Match` version token would close the lost-update race properly.
</content>
