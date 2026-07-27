"""
Database Interface
------------------
All agent output storage, on either backend (see core/db_backend.py):

  sqlite  — default, db/pipeline.db. Zero setup, used by the CLI and tests.
  mysql   — the multi-user Docker deployment.

Everything routes through the generic helpers below (insert/fetch/update/
count/query/execute), so the SQL dialect lives in one place. Do not open a
raw connection to pipeline state — db/conceptnet.db is the sole exception,
being a read-only reference corpus.

Tables:
  - runs             Pipeline run registry
  - sources          active / seminal / historical entries
  - dead_links       Dead link archive
  - gaps             Gaper output
  - implications     Vision output
  - proposals        Theorist output
  - evaluations      Rude output
  - syntheses        Synthesizer output
  - directions       Thinker output
  - artifacts        Scribe output
  - seminal_bank     Grounder proposed themes
"""

import json
import logging
import re
import threading as _threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Any

from core import db_backend

logger = logging.getLogger(__name__)

# SQLite backend only. Ignored when the MySQL backend is selected.
DB_PATH = Path(__file__).parent.parent / "db" / "pipeline.db"


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_connection():
    """
    Raw connection for the current backend.

    Prefer db_backend.cursor() or the generic helpers below — this exists for
    the few callers that need direct control, and the caller must close it.
    """
    return db_backend.get_backend().connect()


def connection():
    """Transactional context manager — commits on success, rolls back on error."""
    return db_backend.connection()


def backend_name() -> str:
    return db_backend.get_backend().name


def use_sqlite_file(path) -> None:
    """
    Point the data layer at a specific SQLite file.

    For the offline tools in tools/, which accept a --db argument, and for
    tests. Has no effect on an already-running MySQL deployment beyond
    switching this process over.
    """
    global DB_PATH
    import os
    DB_PATH = Path(path)
    os.environ["SEEKER_DB_BACKEND"] = "sqlite"
    db_backend.reset_backend()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
-- Pipeline runs registry
CREATE TABLE IF NOT EXISTS runs (
    run_id          {ID} PRIMARY KEY,
    problem         {LONGTEXT} NOT NULL,
    created_at      {TEXT} NOT NULL,
    status          {KEY} DEFAULT 'active',
    break0_done     {INT} DEFAULT 0,
    break1_done     {INT} DEFAULT 0,
    break2_done     {INT} DEFAULT 0,
    completed_at    {TEXT},
    previous_run_id {ID}
);

-- Sources: current (Social), seminal (Grounder), historical (Historian)
CREATE TABLE IF NOT EXISTS sources (
    source_id       {ID} PRIMARY KEY,
    title           {TEXT} NOT NULL,
    authors         {TEXT},                -- JSON array
    year            {INT},
    source_name     {TEXT},
    doi             {TEXT},
    abstract        {LONGTEXT},
    active_link     {TEXT},
    theme_tags      {TEXT},                -- JSON array
    type            {KEY} NOT NULL,       -- current / seminal / historical
    relevance_rating {TEXT},              -- High / Medium / Low (current)
    relevance_reason {TEXT},
    seminal_reason  {TEXT},               -- Grounder
    historical_reason {TEXT},             -- Historian
    phase_tag       {TEXT},               -- Historian phase classification
    intersection_tags {TEXT},             -- JSON array
    added_by        {TEXT},
    date_collected  {TEXT},
    last_checked    {TEXT},
    link_status     {KEY} DEFAULT 'active', -- active / redirected / dead / flagged
    run_id          {ID},
    previously_seen {INT} DEFAULT 0         -- F5: 1 if this source also appeared in a previous run
);

-- Dead links archive
CREATE TABLE IF NOT EXISTS dead_links (
    dead_id         {ID} PRIMARY KEY,
    source_id       {ID},
    title           {TEXT},
    original_link   {TEXT},
    theme_tags      {TEXT},
    type            {KEY},
    date_collected  {TEXT},
    date_confirmed_dead {TEXT},
    last_active     {TEXT}
);

-- Gaper: gaps
CREATE TABLE IF NOT EXISTS gaps (
    gap_id          {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    problem_origin  {TEXT},
    gap_type        {TEXT},               -- unstudied / incomplete / contradicted etc.
    description     {LONGTEXT} NOT NULL,
    significance    {KEY},               -- High / Medium / Low
    significance_reason {TEXT},
    primary_evaluation {TEXT},            -- answered / partial / unanswered
    references_grounder {TEXT},           -- JSON array of source_ids
    references_historian {TEXT},          -- JSON array of source_ids
    references_social {TEXT},             -- JSON array of source_ids
    dead_end_revisit {INT} DEFAULT 0,
    recurring_pattern {INT} DEFAULT 0,
    recurring_reason {TEXT},
    added_by        {KEY} DEFAULT 'Gaper',
    date_identified {TEXT},
    status          {KEY} DEFAULT 'open' -- open / addressed / resolved / deferred
);

-- Vision: implications
CREATE TABLE IF NOT EXISTS implications (
    implication_id  {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    problem_origin  {TEXT},
    implication     {LONGTEXT} NOT NULL,
    implication_type {TEXT},              -- direct / logical_chain / second_order etc.
    strength        {TEXT},               -- Strong / Moderate / Speculative
    strength_reason {TEXT},
    scope           {TEXT},               -- immediate / second_order
    derived_grounder {TEXT},              -- JSON array
    derived_historian {TEXT},             -- JSON array
    derived_gaper   {TEXT},               -- JSON array
    derived_social  {TEXT},               -- JSON array
    hidden_assumption {INT} DEFAULT 0,
    assumption_note {TEXT},
    currently_pursued {INT} DEFAULT 0,
    pursuit_reference {TEXT},
    added_by        {KEY} DEFAULT 'Vision',
    date_identified {TEXT},
    status          {KEY} DEFAULT 'active'
);

-- Theorist: proposals
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id     {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    problem_origin  {TEXT},
    proposal        {LONGTEXT} NOT NULL,
    proposal_type   {TEXT},               -- novel / extension / revival / hybrid
    addresses_gaps  {TEXT},               -- JSON array of gap_ids
    addresses_implications {TEXT},        -- JSON array of implication_ids
    addresses_foundations {TEXT},         -- JSON array of source_ids
    assumptions     {TEXT},               -- JSON array
    requirements    {TEXT},               -- JSON array
    predictions     {TEXT},               -- JSON array
    dead_end_reassessment {INT} DEFAULT 0,
    dead_end_reference {TEXT},
    dead_end_reason {TEXT},
    interdependencies {TEXT},             -- JSON array of proposal_ids
    promise_rating  {TEXT},               -- High / Medium / Low
    promise_reason  {TEXT},
    novel_vs_extension {TEXT},
    scope           {TEXT},
    added_by        {KEY} DEFAULT 'Theorist',
    date_proposed   {TEXT},
    status          {KEY} DEFAULT 'proposed'
);

-- Rude: evaluations
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id   {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    proposal_id     {ID} NOT NULL,
    problem_origin  {TEXT},
    verdict         {TEXT} NOT NULL,      -- feasible / partially_feasible / unfeasible / insufficient_evidence
    verdict_reason  {TEXT},
    weakest_empirical_link {TEXT},
    dead_end_references {TEXT},           -- JSON array
    social_evidence_references {TEXT},    -- JSON array
    evidence_to_change_verdict {TEXT},
    added_by        {KEY} DEFAULT 'Rude',
    date_evaluated  {TEXT},
    status          {KEY} DEFAULT 'active'
);

-- Synthesizer: research narratives
CREATE TABLE IF NOT EXISTS syntheses (
    synthesis_id    {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    problem_origin  {TEXT},
    sharpened_problem {LONGTEXT},
    trajectory_statement {LONGTEXT},
    key_tensions    {TEXT},               -- JSON array
    override_log    {TEXT},               -- JSON array
    viable_proposal_ids {TEXT},           -- JSON array
    top_gap_ids     {TEXT},               -- JSON array
    top_implication_ids {TEXT},           -- JSON array
    full_narrative  {LONGTEXT},               -- full text of the narrative
    added_by        {KEY} DEFAULT 'Synthesizer',
    date_produced   {TEXT},
    status          {KEY} DEFAULT 'draft'
);

-- Thinker: new directions
CREATE TABLE IF NOT EXISTS directions (
    direction_id    {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    problem_origin  {TEXT},
    direction       {LONGTEXT} NOT NULL,
    direction_type  {TEXT},               -- new_research / new_framing / adjacent_field etc.
    grounding_reference {TEXT},
    distance_rating {TEXT},               -- Near / Mid / Far
    synthesis_id    {ID},
    added_by        {KEY} DEFAULT 'Thinker',
    date_proposed   {TEXT},
    status          {KEY} DEFAULT 'proposed'
);

-- Scribe: produced artifacts
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id     {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    problem_origin  {TEXT},
    output_type     {TEXT},               -- blog_post / research_brief / paper_section etc.
    format          {TEXT},               -- md / tex
    title           {TEXT},
    audience        {TEXT},
    synthesis_id    {ID},
    directions_used {TEXT},               -- JSON array of direction_ids
    file_path       {TEXT},
    word_count      {INT},
    added_by        {KEY} DEFAULT 'Scribe',
    date_produced   {TEXT},
    status          {KEY} DEFAULT 'draft'
);

-- Grounder: proposed themes for seminal bank
CREATE TABLE IF NOT EXISTS seminal_bank (
    bank_id         {ID} PRIMARY KEY,
    proposed_theme  {TEXT} NOT NULL,
    proposed_by     {KEY} DEFAULT 'Grounder',
    problem_origin  {TEXT},
    reason          {TEXT},
    suggested_keywords {TEXT},            -- JSON array
    suggested_sources {TEXT},             -- JSON array
    date_proposed   {TEXT},
    status          {KEY} DEFAULT 'pending_review' -- pending_review / approved / rejected
);

-- Human-in-the-loop break instructions, persisted verbatim.
-- Without this, resuming a run loses the researcher's steering input.
CREATE TABLE IF NOT EXISTS break_instructions (
    instruction_id  {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    break_num       {INT} NOT NULL,
    instructions    {LONGTEXT} NOT NULL,       -- raw text, exactly as the human wrote it
    contradictions  {KEY} DEFAULT '[]',   -- JSON array of contradiction notices
    source          {KEY} DEFAULT 'cli',  -- cli / web
    created_at      {TEXT} NOT NULL,
    UNIQUE (run_id, break_num)
);

-- Per-source outcome for each (run, source, agent) search. Makes a failed
-- source visible — today a source that returned [] because it was down is
-- indistinguishable from one that returned [] because nothing matched
-- (review E4). One row per (run_id, source_id, agent) — updated in place.
CREATE TABLE IF NOT EXISTS source_health (
    health_id       {ID} PRIMARY KEY,
    run_id          {ID} NOT NULL,
    source_id       {KEY} NOT NULL,
    agent           {KEY} NOT NULL,            -- social / grounder / historian
    status          {KEY} NOT NULL,            -- ok / degraded / failed / skipped
    results_returned {INT} DEFAULT 0,
    calls_made      {INT} DEFAULT 0,
    retries         {INT} DEFAULT 0,
    last_error      {TEXT},
    checked_at      {TEXT} NOT NULL,
    UNIQUE (run_id, source_id, agent)
);

-- Global daily call counts per (date, source, user). OpenAlex's 100k/day is
-- a global limit across all of a user's runs, not per-run; tracking it in the
-- DB makes it shared across workers and survives restarts (review R4).
-- exhausted_until marks a source that 429'd with a long Retry-After, so a
-- fresh run skips it instead of burning its retry budget.
CREATE TABLE IF NOT EXISTS source_call_log (
    log_id          {ID} PRIMARY KEY,
    log_date        {KEY} NOT NULL,            -- YYYY-MM-DD (UTC)
    source_id       {KEY} NOT NULL,
    user_id         {KEY} NOT NULL DEFAULT 'anon',
    calls           {INT} DEFAULT 0,
    exhausted_until {TEXT},                    -- ISO timestamp or NULL
    UNIQUE (log_date, source_id, user_id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_break_instr_run  ON break_instructions(run_id);
CREATE INDEX IF NOT EXISTS idx_sources_type     ON sources(type);
CREATE INDEX IF NOT EXISTS idx_sources_run      ON sources(run_id);
CREATE INDEX IF NOT EXISTS idx_gaps_run         ON gaps(run_id);
CREATE INDEX IF NOT EXISTS idx_gaps_significance ON gaps(significance);
CREATE INDEX IF NOT EXISTS idx_implications_run ON implications(run_id);
CREATE INDEX IF NOT EXISTS idx_proposals_run    ON proposals(run_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_run  ON evaluations(run_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_proposal ON evaluations(proposal_id);
CREATE INDEX IF NOT EXISTS idx_syntheses_run    ON syntheses(run_id);
CREATE INDEX IF NOT EXISTS idx_directions_run   ON directions(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run    ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_source_health_run ON source_health(run_id);
CREATE INDEX IF NOT EXISTS idx_source_call_log  ON source_call_log(log_date, source_id, user_id);

-- Per-LLM-call token usage tracking (F10). One row per successful call.
CREATE TABLE IF NOT EXISTS llm_usage (
    usage_id         {ID} PRIMARY KEY,
    run_id           {ID},
    agent_name       {KEY} NOT NULL,
    provider         {KEY} NOT NULL,
    model            {TEXT} NOT NULL,
    prompt_tokens    {INT} DEFAULT 0,
    completion_tokens {INT} DEFAULT 0,
    total_tokens     {INT} DEFAULT 0,
    timestamp        {TEXT} NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_run   ON llm_usage(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_usage(agent_name);

-- Source blacklist (F8): user-marked DOIs / URLs / title substrings that
-- should never enter a run. Checked at insert time by is_source_blacklisted.
CREATE TABLE IF NOT EXISTS source_blacklist (
    blacklist_id   {ID} PRIMARY KEY,
    user_id        {ID} NOT NULL DEFAULT 'anon',
    match_type     {KEY} NOT NULL,            -- doi | url | title_substring
    match_value    {KEY} NOT NULL,            -- indexed (UNIQUE), so {KEY} not {TEXT}
    reason         {TEXT},
    created_at     {TEXT} NOT NULL,
    UNIQUE (user_id, match_type, match_value)
);
CREATE INDEX IF NOT EXISTS idx_source_blacklist_user ON source_blacklist(user_id);
"""


def init_db():
    """Initialize database — create all tables if they don't exist."""
    backend = db_backend.get_backend()
    backend.init_schema(SCHEMA)
    # These live in their own modules but share the same database
    from core.argument_tree import init_tree_table
    from core.pipeline import init_steps_table
    init_tree_table()
    init_steps_table()
    logger.info(f"Database initialized ({backend.name}) at {backend.target}")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)

def _from_json(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def insert(table: str, data: dict) -> bool:
    """Generic upsert into any table."""
    backend = db_backend.get_backend()
    sql = backend.upsert_sql(table, list(data.keys()))
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, list(data.values()))
        return True
    except Exception as e:
        logger.error(f"Insert into {table} failed: {e}")
        return False


_ORDER_TERM = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s+(ASC|DESC))?$", re.IGNORECASE)


def order_clause(order_by: Optional[str]) -> str:
    """
    Render a validated ORDER BY.

    Column names cannot be parameterised, so each comma-separated term must
    match a plain identifier with an optional ASC/DESC. Anything else raises
    rather than reaching the database.
    """
    if not order_by:
        return ""
    terms = [t.strip() for t in order_by.split(",") if t.strip()]
    for term in terms:
        if not _ORDER_TERM.match(term):
            raise ValueError(f"Unsafe order_by term: {term!r}")
    return " ORDER BY " + ", ".join(terms)


def fetch(table: str, where: dict = None, limit: int = None,
          order_by: str = None, offset: int = None) -> list[dict]:
    """
    Generic fetch from any table.

    order_by matters more than it looks: without it the storage engine picks
    the order, and callers that slice the result (the context builder, admin
    promotion) end up selecting an arbitrary subset. See review V1 and S2.
    """
    ph = db_backend.placeholder()
    sql = f"SELECT * FROM {table}"
    params = []
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = {ph}" for k in where)
        params = list(where.values())
    sql += order_clause(order_by)
    if limit:
        sql += f" LIMIT {int(limit)}"
        if offset:
            sql += f" OFFSET {int(offset)}"
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, params)
            return db_backend.rows_to_dicts(cur.fetchall())
    except Exception as e:
        logger.error(f"Fetch from {table} failed: {e}")
        return []


def insert_unique(table: str, data: dict) -> bool:
    """
    Plain INSERT — returns False if a UNIQUE constraint rejects the row.

    `insert()` upserts, which is right for records keyed by their own
    identity but wrong when the constraint is the point: an upsert on a
    conflict silently *replaces* the row that was already there. Used by the
    job queue, where a duplicate insert must lose rather than clobber the
    job another process just queued (review C5).
    """
    backend = db_backend.get_backend()
    sql = backend.insert_sql(table, list(data.keys()))
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, list(data.values()))
        return True
    except Exception as e:
        logger.debug(f"Unique insert into {table} rejected: {e}")
        return False


def accumulate(table: str, data: dict, conflict_cols: list[str],
               add_columns: list[str] = (), expr_columns: dict = None) -> bool:
    """
    Upsert that adds to counter columns rather than overwriting them.

    One statement, so two workers incrementing the same row cannot lose each
    other's update the way a read-then-write pair does (review C6).
    """
    backend = db_backend.get_backend()
    sql = backend.accumulate_sql(table, list(data.keys()), conflict_cols,
                                 add_columns, expr_columns)
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, list(data.values()))
        return True
    except Exception as e:
        logger.error(f"Accumulate into {table} failed: {e}")
        return False


def execute_rowcount(sql: str, params: tuple = ()) -> int:
    """
    Run a write statement and report how many rows it touched.

    Lets a caller use an UPDATE as a compare-and-swap: zero rows means
    somebody else got there first.
    """
    ph = db_backend.placeholder()
    if ph != "?":
        sql = sql.replace("?", ph)
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.rowcount or 0)
    except Exception as e:
        logger.error(f"Execute failed: {e}\n  SQL: {sql}")
        return 0


def update(table: str, data: dict, where: dict) -> bool:
    """Generic update on any table."""
    ph = db_backend.placeholder()
    set_clause   = ", ".join(f"{k} = {ph}" for k in data)
    where_clause = " AND ".join(f"{k} = {ph}" for k in where)
    sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, list(data.values()) + list(where.values()))
        return True
    except Exception as e:
        logger.error(f"Update {table} failed: {e}")
        return False


def count(table: str, where: dict = None) -> int:
    """Count rows in a table."""
    ph = db_backend.placeholder()
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    params = []
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = {ph}" for k in where)
        params = list(where.values())
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return 0
            # sqlite3.Row indexes by position; DictCursor by name.
            return int(dict(row)["n"]) if not isinstance(row, dict) else int(row["n"])
    except Exception as e:
        logger.error(f"Count {table} failed: {e}")
        return 0


def query(sql: str, params: tuple = ()) -> list[dict]:
    """
    Run a read query written with '?' placeholders, translated per backend.
    For the handful of call sites that need SQL the generic helpers cannot express.
    """
    ph = db_backend.placeholder()
    if ph != "?":
        sql = sql.replace("?", ph)
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, params)
            return db_backend.rows_to_dicts(cur.fetchall())
    except Exception as e:
        logger.error(f"Query failed: {e}\n  SQL: {sql}")
        return []


def execute(sql: str, params: tuple = ()) -> bool:
    """Run a write statement written with '?' placeholders, translated per backend."""
    ph = db_backend.placeholder()
    if ph != "?":
        sql = sql.replace("?", ph)
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, params)
        return True
    except Exception as e:
        logger.error(f"Execute failed: {e}\n  SQL: {sql}")
        return False


# ---------------------------------------------------------------------------
# Run management
# ---------------------------------------------------------------------------

def create_run(run_id: str, problem: str, previous_run_id: str = None) -> bool:
    data = {
        "run_id":     run_id,
        "problem":    problem,
        "created_at": _now(),
        "status":     "active",
    }
    if previous_run_id:
        data["previous_run_id"] = previous_run_id
    return insert("runs", data)


def get_run(run_id: str) -> Optional[dict]:
    rows = fetch("runs", {"run_id": run_id})
    return rows[0] if rows else None


def get_previous_run_id(run_id: str) -> Optional[str]:
    """Get the previous_run_id for a run, if set (F5)."""
    run = get_run(run_id)
    if not run:
        return None
    get = (lambda k, _r=run: _r[k] if k in _r.keys() else None) \
          if not isinstance(run, dict) else (lambda k, _r=run: _r.get(k))
    return get("previous_run_id") or None


def get_previous_run_source_keys(run_id: str) -> set[tuple[str, str]]:
    """
    Return a set of (doi, normalized_title) tuples for all sources in the
    previous run (F5). Used to flag sources that were already seen.

    DOI is normalized (lowercased, stripped of URL prefix). Title is
    lowercased and stripped of whitespace/punctuation.
    """
    prev_id = get_previous_run_id(run_id)
    if not prev_id:
        return set()

    rows = fetch("sources", {"run_id": prev_id})
    keys = set()
    for r in rows:
        get = (lambda k, _r=r: _r[k] if k in _r.keys() else None) \
              if not isinstance(r, dict) else (lambda k, _r=r: _r.get(k))
        doi = (get("doi") or "").strip().lower()
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]
        if doi.startswith("http://doi.org/"):
            doi = doi[len("http://doi.org/"):]
        title = _normalize_title(get("title") or "")
        if doi or title:
            keys.add((doi, title))
    return keys


def _normalize_title(title: str) -> str:
    """Normalize a title for dedup comparison: lowercase, strip punctuation."""
    import re
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def mark_previously_seen(run_id: str, source_keys: set[tuple[str, str]]) -> int:
    """
    Mark sources in this run that also appear in the previous run's key set.
    Returns the count of sources marked.

    Called after Social/Grounder insert their sources for the run.
    """
    if not source_keys:
        return 0
    rows = fetch("sources", {"run_id": run_id})
    count = 0
    for r in rows:
        get = (lambda k, _r=r: _r[k] if k in _r.keys() else None) \
              if not isinstance(r, dict) else (lambda k, _r=r: _r.get(k))
        sid = get("source_id")
        doi = (get("doi") or "").strip().lower()
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]
        if doi.startswith("http://doi.org/"):
            doi = doi[len("http://doi.org/"):]
        title = _normalize_title(get("title") or "")
        if (doi, title) in source_keys and (doi or title):
            update("sources", {"previously_seen": 1}, {"source_id": sid})
            count += 1
    return count


def update_run_status(run_id: str, status: str) -> bool:
    data = {"status": status}
    if status == "completed":
        data["completed_at"] = _now()
    return update("runs", data, {"run_id": run_id})


def mark_break_done(run_id: str, break_num: int) -> bool:
    col = f"break{break_num}_done"
    return update("runs", {col: 1}, {"run_id": run_id})


# ---------------------------------------------------------------------------
# Break instructions — the human's steering input, persisted across resumes
# ---------------------------------------------------------------------------

def save_break_instructions(
    run_id: str,
    break_num: int,
    instructions: str,
    contradictions: list = None,
    source: str = "cli",
) -> bool:
    """Persist a break's instructions verbatim. Replaces any prior submission."""
    from core.utils import generate_id
    return insert("break_instructions", {
        "instruction_id": generate_id("BRK"),
        "run_id":         run_id,
        "break_num":      break_num,
        "instructions":   instructions,
        "contradictions": _json(contradictions or []),
        "source":         source,
        "created_at":     _now(),
    })


def get_break_instructions(run_id: str, break_num: int) -> Optional[dict]:
    """Retrieve stored instructions for a break, or None if never submitted."""
    rows = fetch("break_instructions", {"run_id": run_id, "break_num": break_num})
    if not rows:
        return None
    row = rows[0]
    row["contradictions"] = _from_json(row.get("contradictions"))
    return row


# ---------------------------------------------------------------------------
# Source management (Social / Grounder / Historian)
# ---------------------------------------------------------------------------

def upsert_source(source: dict) -> bool:
    """Insert or update a source entry.

    Checks the source blacklist (F8) first — if the source's DOI, URL, or
    title matches a blacklist entry for the run's owner, it is silently
    dropped. Returns False when dropped.
    """
    # F8: blacklist check. Look up the run's owner to scope the blacklist.
    run_id = source.get("run_id")
    if run_id:
        try:
            from core import users
            owner = users.run_owner(run_id)
            user_id = owner.get("user_id") if owner else "anon"
        except Exception:
            user_id = "anon"
        matched, reason = is_source_blacklisted(
            user_id,
            doi=source.get("doi") or "",
            url=source.get("active_link") or "",
            title=source.get("title") or "",
        )
        if matched:
            logger.info(
                f"[F8] Dropping blacklisted source '{(source.get('title') or '')[:60]}' "
                f"for run {run_id} ({reason or 'no reason given'})"
            )
            return False
    for field in ["authors", "theme_tags", "intersection_tags"]:
        if field in source:
            source[field] = _json(source[field])
    return insert("sources", source)


# ---------------------------------------------------------------------------
# Ranked reads (review V1)
#
# Every one of these columns is a judgement the pipeline spent a model call
# to produce. Reading them back without an ORDER BY and then slicing — which
# is what the context builder used to do — throws that judgement away and
# hands the writing agents an arbitrary subset. The rankings below are the
# orders those slices should be taken in.
#
# NULL sorts last everywhere: an unrated source is not a mid-ranked one
# (review V5).
# ---------------------------------------------------------------------------

RELEVANCE_ORDER = ("CASE relevance_rating WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 "
                   "WHEN 'Low' THEN 3 ELSE 4 END, year DESC")
SIGNIFICANCE_ORDER = ("CASE significance WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 "
                      "WHEN 'Low' THEN 3 ELSE 4 END")
STRENGTH_ORDER = ("CASE strength WHEN 'Strong' THEN 1 WHEN 'Moderate' THEN 2 "
                  "WHEN 'Speculative' THEN 3 ELSE 4 END")
PROMISE_ORDER = ("CASE promise_rating WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 "
                 "WHEN 'Low' THEN 3 ELSE 4 END")


def _ranked(table: str, where: dict, order_sql: str,
            limit: int = None) -> list[dict]:
    """Fetch rows in a fixed ranking. order_sql is module-controlled, never
    caller input."""
    ph = db_backend.placeholder()
    sql = f"SELECT * FROM {table}"
    params: list = []
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = {ph}" for k in where)
        params = list(where.values())
    sql += f" ORDER BY {order_sql}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        with db_backend.cursor() as cur:
            cur.execute(sql, params)
            return db_backend.rows_to_dicts(cur.fetchall())
    except Exception as e:
        logger.error(f"Ranked fetch from {table} failed: {e}")
        return []


def get_sources_by_type(source_type: str, run_id: str = None,
                        ranked: bool = False, limit: int = None) -> list[dict]:
    where = {"type": source_type}
    if run_id:
        where["run_id"] = run_id
    if ranked:
        return _ranked("sources", where, RELEVANCE_ORDER, limit)
    return fetch("sources", where, limit=limit)


def count_by(table: str, column: str, where: dict) -> dict:
    """
    Group-count one column — for "of 356 gaps, 12 High / 88 Medium".

    `column` is module-controlled, never caller input.
    """
    ph = db_backend.placeholder()
    sql = f"SELECT {column} AS k, COUNT(*) AS n FROM {table}"
    params: list = []
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = {ph}" for k in where)
        params = list(where.values())
    sql += f" GROUP BY {column}"
    out: dict = {}
    for row in query(sql.replace(ph, "?"), tuple(params)):
        out[row.get("k") or "unrated"] = int(row.get("n") or 0)
    return out


def archive_dead_link(source: dict) -> bool:
    """Move a dead source to dead_links table."""
    dead = {
        "dead_id":             f"DEAD-{source['source_id']}",
        "source_id":           source["source_id"],
        "title":               source.get("title"),
        "original_link":       source.get("active_link"),
        "theme_tags":          source.get("theme_tags"),
        "type":                source.get("type"),
        "date_collected":      source.get("date_collected"),
        "date_confirmed_dead": _now(),
        "last_active":         source.get("last_checked")
    }
    ok = insert("dead_links", dead)
    if ok:
        update("sources", {"link_status": "dead"}, {"source_id": source["source_id"]})
    return ok


# ---------------------------------------------------------------------------
# Gap management (Gaper)
# ---------------------------------------------------------------------------

def insert_gap(gap: dict) -> bool:
    for field in ["references_grounder", "references_historian", "references_social"]:
        if field in gap:
            gap[field] = _json(gap[field])
    if "date_identified" not in gap:
        gap["date_identified"] = _now()
    return insert("gaps", gap)


def get_gaps(run_id: str, significance: str = None,
             ranked: bool = False, limit: int = None) -> list[dict]:
    where = {"run_id": run_id}
    if significance:
        where["significance"] = significance
    if ranked:
        return _ranked("gaps", where, SIGNIFICANCE_ORDER, limit)
    return fetch("gaps", where, limit=limit)


# ---------------------------------------------------------------------------
# Implication management (Vision)
# ---------------------------------------------------------------------------

def insert_implication(imp: dict) -> bool:
    for field in ["derived_grounder", "derived_historian", "derived_gaper", "derived_social"]:
        if field in imp:
            imp[field] = _json(imp[field])
    if "date_identified" not in imp:
        imp["date_identified"] = _now()
    return insert("implications", imp)


def get_implications(run_id: str, strength: str = None,
                     ranked: bool = False, limit: int = None) -> list[dict]:
    where = {"run_id": run_id}
    if strength:
        where["strength"] = strength
    if ranked:
        return _ranked("implications", where, STRENGTH_ORDER, limit)
    return fetch("implications", where, limit=limit)


# ---------------------------------------------------------------------------
# Proposal management (Theorist)
# ---------------------------------------------------------------------------

def insert_proposal(proposal: dict) -> bool:
    for field in ["addresses_gaps", "addresses_implications", "addresses_foundations",
                  "assumptions", "requirements", "predictions", "interdependencies"]:
        if field in proposal:
            proposal[field] = _json(proposal[field])
    if "date_proposed" not in proposal:
        proposal["date_proposed"] = _now()
    return insert("proposals", proposal)


def get_proposals(run_id: str, status: str = None,
                  ranked: bool = False, limit: int = None) -> list[dict]:
    where = {"run_id": run_id}
    if status:
        where["status"] = status
    if ranked:
        return _ranked("proposals", where, PROMISE_ORDER, limit)
    return fetch("proposals", where, limit=limit)


# ---------------------------------------------------------------------------
# Evaluation management (Rude)
# ---------------------------------------------------------------------------

def insert_evaluation(evaluation: dict) -> bool:
    for field in ["dead_end_references", "social_evidence_references"]:
        if field in evaluation:
            evaluation[field] = _json(evaluation[field])
    if "date_evaluated" not in evaluation:
        evaluation["date_evaluated"] = _now()
    # Update proposal status
    if "proposal_id" in evaluation:
        verdict_to_status = {
            "feasible":              "feasible",
            "partially_feasible":    "feasible",
            "unfeasible":            "rejected",
            "insufficient_evidence": "deferred"
        }
        new_status = verdict_to_status.get(evaluation.get("verdict"), "under_review")
        update("proposals", {"status": new_status}, {"proposal_id": evaluation["proposal_id"]})
    return insert("evaluations", evaluation)


def get_evaluations(run_id: str, verdict: str = None) -> list[dict]:
    where = {"run_id": run_id}
    if verdict:
        where["verdict"] = verdict
    return fetch("evaluations", where)


# ---------------------------------------------------------------------------
# Synthesis management (Synthesizer)
# ---------------------------------------------------------------------------

def insert_synthesis(synthesis: dict) -> bool:
    for field in ["key_tensions", "override_log", "viable_proposal_ids",
                  "top_gap_ids", "top_implication_ids"]:
        if field in synthesis:
            synthesis[field] = _json(synthesis[field])
    if "date_produced" not in synthesis:
        synthesis["date_produced"] = _now()
    return insert("syntheses", synthesis)


def get_synthesis(run_id: str) -> Optional[dict]:
    rows = fetch("syntheses", {"run_id": run_id})
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Direction management (Thinker)
# ---------------------------------------------------------------------------

def insert_direction(direction: dict) -> bool:
    if "date_proposed" not in direction:
        direction["date_proposed"] = _now()
    return insert("directions", direction)


def get_directions(run_id: str) -> list[dict]:
    return fetch("directions", {"run_id": run_id})


# ---------------------------------------------------------------------------
# Artifact management (Scribe)
# ---------------------------------------------------------------------------

def insert_artifact(artifact: dict) -> bool:
    if field := artifact.get("directions_used"):
        artifact["directions_used"] = _json(field)
    if "date_produced" not in artifact:
        artifact["date_produced"] = _now()
    return insert("artifacts", artifact)


def get_artifacts(run_id: str) -> list[dict]:
    return fetch("artifacts", {"run_id": run_id})


# ---------------------------------------------------------------------------
# LLM usage tracking (F10)
# ---------------------------------------------------------------------------

def record_llm_usage(run_id: str, agent_name: str, provider: str,
                     model: str, prompt_tokens: int = 0,
                     completion_tokens: int = 0) -> bool:
    """Record one LLM call's token usage."""
    from core.utils import generate_id
    total = (prompt_tokens or 0) + (completion_tokens or 0)
    return insert("llm_usage", {
        "usage_id":          generate_id("USE"),
        "run_id":            run_id,
        "agent_name":        agent_name,
        "provider":          provider,
        "model":             model,
        "prompt_tokens":     prompt_tokens or 0,
        "completion_tokens": completion_tokens or 0,
        "total_tokens":      total,
        "timestamp":         _now(),
    })


def get_llm_usage(run_id: str) -> list[dict]:
    """All usage rows for a run, ordered by time."""
    return fetch("llm_usage", {"run_id": run_id})


def get_llm_usage_summary(run_id: str) -> dict:
    """
    Aggregated usage by agent and by model.

    Returns:
      {
        "total_tokens": int,
        "total_prompt": int,
        "total_completion": int,
        "total_calls": int,
        "by_agent": { "<agent>": {calls, prompt, completion, total} },
        "by_model":  { "<provider:model>": {calls, prompt, completion, total} },
      }
    """
    rows = fetch("llm_usage", {"run_id": run_id})
    by_agent: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    total_prompt = 0
    total_completion = 0
    total_calls = 0

    for r in rows:
        get = (lambda k, _r=r: _r[k] if k in _r.keys() else None) \
              if not isinstance(r, dict) else (lambda k, _r=r: _r.get(k))
        agent = get("agent_name") or "unknown"
        prov = get("provider") or "unknown"
        model = get("model") or "unknown"
        pt = get("prompt_tokens") or 0
        ct = get("completion_tokens") or 0
        tt = get("total_tokens") or (pt + ct)

        total_prompt += pt
        total_completion += ct
        total_calls += 1

        a = by_agent.setdefault(agent, {"calls": 0, "prompt": 0, "completion": 0, "total": 0})
        a["calls"] += 1
        a["prompt"] += pt
        a["completion"] += ct
        a["total"] += tt

        key = f"{prov}:{model}"
        m = by_model.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0, "total": 0})
        m["calls"] += 1
        m["prompt"] += pt
        m["completion"] += ct
        m["total"] += tt

    return {
        "total_tokens": total_prompt + total_completion,
        "total_prompt": total_prompt,
        "total_completion": total_completion,
        "total_calls": total_calls,
        "by_agent": by_agent,
        "by_model": by_model,
    }


# ---------------------------------------------------------------------------
# Source blacklist (F8)
# ---------------------------------------------------------------------------

def add_to_blacklist(user_id: str, match_type: str, match_value: str,
                     reason: str = "") -> bool:
    """
    Add an entry to the source blacklist.

    match_type: 'doi' | 'url' | 'title_substring'
    match_value: the DOI / URL / title substring to match (case-insensitive)
    """
    from core.utils import generate_id
    if match_type not in ("doi", "url", "title_substring"):
        raise ValueError(f"Invalid match_type: {match_type}")
    if not match_value or not match_value.strip():
        return False
    # Normalize: DOIs and URLs lowercased; title substrings lowercased + stripped
    value = match_value.strip().lower()
    if match_type == "doi":
        value = _normalize_doi(value)
    ok = insert("source_blacklist", {
        "blacklist_id": generate_id("BLK"),
        "user_id":      user_id or "anon",
        "match_type":   match_type,
        "match_value":  value,
        "reason":       reason or "",
        "created_at":   _now(),
    })
    invalidate_blacklist_cache(user_id)
    return ok


def remove_from_blacklist(user_id: str, match_type: str, match_value: str) -> bool:
    """Remove an entry from the source blacklist."""
    value = (match_value or "").strip().lower()
    if match_type == "doi":
        value = _normalize_doi(value)
    ok = execute(
        "DELETE FROM source_blacklist WHERE user_id = ? AND match_type = ? AND match_value = ?",
        (user_id or "anon", match_type, value),
    )
    invalidate_blacklist_cache(user_id)
    return ok


def list_blacklist(user_id: str) -> list[dict]:
    """List a user's blacklist entries."""
    return fetch("source_blacklist", {"user_id": user_id or "anon"})


def _normalize_doi(doi: str) -> str:
    """Lowercase and strip URL prefix from a DOI."""
    d = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d


# The blacklist is read once per source insert, and Social inserts hundreds
# of sources per run — that was 3-4 extra queries per source for a table that
# changes at human speed (review O5). Cached per user and dropped whenever an
# entry is added or removed.
_blacklist_cache: dict[str, list[dict]] = {}
_blacklist_lock = _threading.Lock()


def _blacklist_for(user_id: str) -> list[dict]:
    key = user_id or "anon"
    with _blacklist_lock:
        cached = _blacklist_cache.get(key)
    if cached is not None:
        return cached
    rows = fetch("source_blacklist", {"user_id": key})
    if not rows and key != "anon":
        # Fall back to the shared 'anon' blacklist if a specific user was given
        rows = fetch("source_blacklist", {"user_id": "anon"})
    with _blacklist_lock:
        _blacklist_cache[key] = rows
    return rows


def invalidate_blacklist_cache(user_id: str = None) -> None:
    with _blacklist_lock:
        if user_id is None:
            _blacklist_cache.clear()
        else:
            # An edit to the shared 'anon' list can affect any user's view.
            _blacklist_cache.pop(user_id or "anon", None)
            if (user_id or "anon") == "anon":
                _blacklist_cache.clear()


def is_source_blacklisted(user_id: str, doi: str = "", url: str = "",
                          title: str = "") -> tuple[bool, Optional[str]]:
    """
    Check whether a source matches any blacklist entry for the given user.

    Returns (matched, reason). 'anon' user_id is shared across all runs
    when no specific user is bound.
    """
    rows = _blacklist_for(user_id)
    if not rows:
        return False, None

    norm_doi = _normalize_doi(doi or "")
    norm_url = (url or "").strip().lower()
    norm_title = (title or "").strip().lower()

    for r in rows:
        get = (lambda k, _r=r: _r[k] if k in _r.keys() else None) \
              if not isinstance(r, dict) else (lambda k, _r=r: _r.get(k))
        mt = get("match_type") or ""
        mv = get("match_value") or ""
        reason = get("reason") or ""
        if mt == "doi" and norm_doi and mv == norm_doi:
            return True, reason
        if mt == "url" and norm_url and mv in norm_url:
            return True, reason
        if mt == "title_substring" and norm_title and mv in norm_title:
            return True, reason
    return False, None


# ---------------------------------------------------------------------------
# Seminal bank (Grounder proposals)
# ---------------------------------------------------------------------------

def insert_seminal_proposal(proposal: dict) -> bool:
    for field in ["suggested_keywords", "suggested_sources"]:
        if field in proposal:
            proposal[field] = _json(proposal[field])
    if "date_proposed" not in proposal:
        proposal["date_proposed"] = _now()
    return insert("seminal_bank", proposal)


def get_seminal_bank(status: str = "pending_review") -> list[dict]:
    return fetch("seminal_bank", {"status": status})


# ---------------------------------------------------------------------------
# Source health — per (run, source, agent) outcome (review E4)
#
# Today a source that returned [] because it was down is indistinguishable
# from one that returned [] because nothing matched. This makes the
# distinction visible to the researcher, so they can judge whether the
# Understanding Map at the end is trustworthy.
# ---------------------------------------------------------------------------

_HEALTH_SEVERITY = {"ok": 0, "degraded": 1, "failed": 2, "skipped": 3}

# Rank a status string inside SQL, so "keep the worse of the two" can be
# decided in the same statement that does the accumulation rather than in a
# read-then-write pair that races (review C6).
_SEVERITY_CASE = ("CASE {expr} " +
                  " ".join(f"WHEN '{name}' THEN {rank}"
                           for name, rank in _HEALTH_SEVERITY.items()) +
                  " ELSE 1 END")


def record_source_health(run_id: str, source_id: str, agent: str,
                         status: str, *, results_returned: int = 0,
                         calls_made: int = 0, retries: int = 0,
                         last_error: str = "") -> bool:
    """
    Upsert one source_health row. status: ok / degraded / failed / skipped.

    Counters accumulate across the run and a status is never downgraded — a
    source that failed once stays failed even if a later query succeeds,
    because the researcher needs to know coverage was interrupted. Both rules
    are expressed in the statement so Social's parallel fan-out cannot lose
    tallies.
    """
    from core.utils import generate_id
    row = {
        "health_id":        generate_id("HLTH"),
        "run_id":           run_id,
        "source_id":        source_id,
        "agent":            agent,
        "status":           status,
        "results_returned": results_returned,
        "calls_made":       calls_made,
        "retries":          retries,
        "last_error":       last_error,
        "checked_at":       _now(),
    }
    keep_worse = (
        "CASE WHEN " + _SEVERITY_CASE.format(expr="{new}") +
        " >= " + _SEVERITY_CASE.format(expr="{old}") +
        " THEN {new} ELSE {old} END"
    )
    return accumulate(
        "source_health", row,
        conflict_cols=["run_id", "source_id", "agent"],
        add_columns=["results_returned", "calls_made", "retries"],
        expr_columns={
            "health_id": "{old}",
            "status":    keep_worse,
            # Don't blank a recorded error with a later empty one.
            "last_error": "CASE WHEN {new} = '' THEN {old} ELSE {new} END",
        },
    )


def get_source_health(run_id: str) -> list[dict]:
    """All source_health rows for a run, newest first."""
    rows = fetch("source_health", {"run_id": run_id})
    rows.sort(key=lambda r: r.get("checked_at", ""), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Global daily call log — shared across runs and workers (review R4)
# ---------------------------------------------------------------------------

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def increment_daily_calls(source_id: str, user_id: str = "anon",
                          n: int = 1) -> int:
    """
    Add n to today's call count for (source, user). Returns the new total.

    A single accumulating upsert, not a read followed by a write: the whole
    point of this table is that the daily budget is shared across workers,
    and a read-modify-write pair loses increments exactly when more than one
    worker is running (review C6).
    """
    from core.utils import generate_id
    date = _today_utc()
    accumulate(
        "source_call_log",
        {
            "log_id":    generate_id("SCL"),
            "log_date":  date,
            "source_id": source_id,
            "user_id":   user_id,
            "calls":     n,
        },
        conflict_cols=["log_date", "source_id", "user_id"],
        add_columns=["calls"],
        # Keep the existing log_id on conflict; only the counter moves.
        expr_columns={"log_id": "{old}"},
    )
    return daily_call_count(source_id, user_id)

def daily_call_count(source_id: str, user_id: str = "anon") -> int:
    rows = fetch("source_call_log",
                 {"log_date": _today_utc(), "source_id": source_id, "user_id": user_id})
    return int(rows[0].get("calls", 0)) if rows else 0

def mark_source_exhausted(source_id: str, exhausted_until_iso: str,
                          user_id: str = "anon") -> bool:
    """Record that a source 429'd with a long Retry-After (review R10)."""
    date = _today_utc()
    existing = fetch("source_call_log",
                     {"log_date": date, "source_id": source_id, "user_id": user_id})
    if existing:
        return update("source_call_log", {"exhausted_until": exhausted_until_iso},
                      {"log_date": date, "source_id": source_id, "user_id": user_id})
    from core.utils import generate_id
    insert("source_call_log", {
        "log_id":          generate_id("SCL"),
        "log_date":        date,
        "source_id":       source_id,
        "user_id":         user_id,
        "calls":           0,
        "exhausted_until": exhausted_until_iso,
    })
    return True

def source_exhausted_until(source_id: str, user_id: str = "anon") -> Optional[str]:
    """ISO timestamp the source is exhausted until, or None if not exhausted."""
    rows = fetch("source_call_log",
                 {"log_date": _today_utc(), "source_id": source_id, "user_id": user_id})
    if not rows:
        return None
    until = rows[0].get("exhausted_until")
    if not until:
        return None
    # Stale exhaustion marker — clear it.
    try:
        if datetime.fromisoformat(until) < datetime.now(timezone.utc):
            update("source_call_log", {"exhausted_until": None},
                   {"log_date": _today_utc(), "source_id": source_id, "user_id": user_id})
            return None
    except Exception:
        return None
    return until
