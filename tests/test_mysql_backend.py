"""
MySQL backend.

Runs against a real MySQL server, skipping when one is not configured. Point
it at a scratch database:

    export SEEKER_TEST_MYSQL_URL=mysql://user:pass@127.0.0.1:3306/seeker_test
    pytest tests/test_mysql_backend.py

The schema is dropped and recreated for each test, so never aim this at a
database holding real runs.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

TEST_URL = os.environ.get("SEEKER_TEST_MYSQL_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="SEEKER_TEST_MYSQL_URL not set — no MySQL server to test against",
)


def _use_mysql():
    os.environ["SEEKER_DB_BACKEND"] = "mysql"
    os.environ["MYSQL_URL"] = TEST_URL
    from core import db_backend
    db_backend.reset_backend()
    return db_backend


def _table_names(db_backend) -> list[str]:
    with db_backend.cursor() as cur:
        cur.execute("SELECT table_name AS t FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()")
        return [r["t"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]


@pytest.fixture(scope="session")
def mysql_schema():
    """
    Build the schema once for the session.

    DDL is expensive (seconds per statement on some hosts), so tests clear
    rows between cases rather than dropping and recreating tables.
    """
    pytest.importorskip("pymysql")
    db_backend = _use_mysql()

    # Start from a known-empty schema
    with db_backend.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for name in _table_names(db_backend):
            cur.execute(f"DROP TABLE IF EXISTS `{name}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")

    import core.database as db
    db.init_db()
    yield db
    db_backend.reset_backend()


@pytest.fixture()
def mysql_db(mysql_schema):
    """Empty every table before each test — DML, so fast."""
    from core import db_backend
    _use_mysql()
    with db_backend.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for name in _table_names(db_backend):
            cur.execute(f"DELETE FROM `{name}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    return mysql_schema


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_backend_is_mysql(mysql_db):
    assert mysql_db.backend_name() == "mysql"


def test_all_tables_created(mysql_db):
    from core import db_backend
    with db_backend.cursor() as cur:
        cur.execute("SELECT table_name AS t FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()")
        tables = {r["t"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}

    expected = {
        "runs", "sources", "dead_links", "gaps", "implications", "proposals",
        "evaluations", "syntheses", "directions", "artifacts", "seminal_bank",
        "break_instructions", "argument_tree",
    }
    assert expected <= tables, f"missing: {expected - tables}"


def test_init_db_is_idempotent(mysql_db):
    """Re-running init must not fail on duplicate indexes — MySQL has no
    CREATE INDEX IF NOT EXISTS."""
    mysql_db.init_db()
    mysql_db.init_db()


def test_primary_keys_are_varchar_not_text(mysql_db):
    """MySQL cannot index TEXT without a prefix length."""
    from core import db_backend
    with db_backend.cursor() as cur:
        cur.execute("""
            SELECT table_name AS t, column_name AS c, data_type AS d
            FROM information_schema.columns
            WHERE table_schema = DATABASE() AND column_key = 'PRI'
        """)
        for row in cur.fetchall():
            row = row if isinstance(row, dict) else {"t": row[0], "c": row[1], "d": row[2]}
            assert row["d"] != "text", f"{row['t']}.{row['c']} is TEXT but indexed"


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def test_insert_fetch_update_count(mysql_db):
    db = mysql_db
    assert db.create_run("RUN-MYSQL-1", "A research problem")

    run = db.get_run("RUN-MYSQL-1")
    assert run["problem"] == "A research problem"
    assert run["status"] == "active"

    db.update_run_status("RUN-MYSQL-1", "completed")
    assert db.get_run("RUN-MYSQL-1")["status"] == "completed"
    assert db.count("runs") == 1
    assert db.count("runs", {"status": "completed"}) == 1
    assert db.count("runs", {"status": "active"}) == 0


def test_upsert_replaces_on_duplicate_key(mysql_db):
    """INSERT OR REPLACE became ON DUPLICATE KEY UPDATE — same semantics."""
    db = mysql_db
    db.create_run("RUN-MYSQL-2", "first version")
    db.insert("runs", {
        "run_id": "RUN-MYSQL-2", "problem": "second version",
        "created_at": "2026-07-25T00:00:00+00:00", "status": "active",
    })
    assert db.count("runs", {"run_id": "RUN-MYSQL-2"}) == 1
    assert db.get_run("RUN-MYSQL-2")["problem"] == "second version"


def test_json_roundtrip(mysql_db):
    db = mysql_db
    db.create_run("RUN-MYSQL-3", "p")
    db.upsert_source({
        "source_id": "SRC-1", "title": "A paper", "type": "seminal",
        "run_id": "RUN-MYSQL-3", "authors": ["Ada Lovelace", "Alan Turing"],
        "theme_tags": ["philosophy_of_mind"], "year": 1950,
    })
    sources = db.get_sources_by_type("seminal", "RUN-MYSQL-3")
    assert len(sources) == 1
    assert sources[0]["title"] == "A paper"
    assert sources[0]["year"] == 1950


def test_break_instructions_on_mysql(mysql_db):
    """The UNIQUE (run_id, break_num) constraint must hold on MySQL too."""
    db = mysql_db
    db.create_run("RUN-MYSQL-4", "p")

    db.save_break_instructions("RUN-MYSQL-4", 1, "first answer")
    db.save_break_instructions("RUN-MYSQL-4", 1, "revised answer", ["a contradiction"])

    assert db.count("break_instructions", {"run_id": "RUN-MYSQL-4"}) == 1
    stored = db.get_break_instructions("RUN-MYSQL-4", 1)
    assert stored["instructions"] == "revised answer"
    assert stored["contradictions"] == ["a contradiction"]


def test_query_translates_placeholders(mysql_db):
    db = mysql_db
    db.create_run("RUN-MYSQL-5", "findable")
    rows = db.query("SELECT problem FROM runs WHERE run_id = ?", ("RUN-MYSQL-5",))
    assert rows[0]["problem"] == "findable"


def test_long_text_survives(mysql_db):
    """Narratives exceed TEXT's 64KB limit — they must be LONGTEXT."""
    db = mysql_db
    db.create_run("RUN-MYSQL-6", "p")
    narrative = "x" * 200_000
    db.insert_synthesis({
        "synthesis_id": "SYN-1", "run_id": "RUN-MYSQL-6",
        "full_narrative": narrative, "sharpened_problem": "sharp",
    })
    assert len(db.get_synthesis("RUN-MYSQL-6")["full_narrative"]) == 200_000


# ---------------------------------------------------------------------------
# Argument tree — the module that used to open its own SQLite connection
# ---------------------------------------------------------------------------

def test_argument_tree_on_mysql(mysql_db):
    from core.argument_tree import TreeBuilder

    mysql_db.create_run("RUN-MYSQL-7", "How does identity influence intelligence?")
    tree = TreeBuilder("RUN-MYSQL-7")
    try:
        root = tree.create_root("How does identity influence intelligence?")
        q1 = tree.add_question(root, "What is identity?")
        c1 = tree.add_claim(q1, "Identity is socially constructed", confidence=0.8)

        # get_tree() returns the root node with children nested beneath it
        nested = tree.get_tree()
        assert nested["node_id"] == root
        assert nested["node_type"] == "root"

        question = nested["children"][0]
        assert question["node_id"] == q1
        assert question["depth"] == 1

        claim = question["children"][0]
        assert claim["node_id"] == c1
        assert claim["depth"] == 2
        assert claim["node_type"] == "claim"
        assert claim["confidence"] == pytest.approx(0.8)
        # JSON columns must survive the round trip on MySQL too
        assert claim["source_ids"] == []
        assert isinstance(claim["metadata"], dict)
    finally:
        tree.close()


def test_audit_note_reads_fields_by_name(mysql_db):
    """
    add_audit_note() reads the target's prior status/confidence via
    _get_field, which indexed rows positionally — fine on sqlite3.Row,
    a KeyError on MySQL's dict rows.
    """
    from core.argument_tree import TreeBuilder

    mysql_db.create_run("RUN-MYSQL-8", "p")
    tree = TreeBuilder("RUN-MYSQL-8")
    try:
        root = tree.create_root("p")
        claim = tree.add_claim(root, "A contested claim", confidence=0.4)

        tree.add_audit_note(claim, "Evidence is thinner than stated",
                            new_status="weak", new_confidence=0.2)

        node = tree.get_node(claim)
        assert node["status"] == "weak"
        assert node["confidence"] == pytest.approx(0.2)

        audit = [n for n in tree.get_nodes_by_type("audit_note")]
        assert len(audit) == 1
        meta = json.loads(audit[0]["metadata"])
        assert meta["previous_status"] == "unsupported"
        assert meta["previous_confidence"] == pytest.approx(0.4)
    finally:
        tree.close()


def test_concept_cache_on_mysql(mysql_db):
    """concept_mapper used to write to its own phantom database."""
    from core import concept_mapper

    concept_mapper._cache_tables_ready = False
    concept_mapper._init_cache_tables()

    mysql_db.insert("concept_cache", {
        "cache_key": "abc123", "term": "consciousness",
        "relations": '[{"rel": "/r/RelatedTo", "target": "qualia"}]',
        "fetched_at": "2026-07-25T00:00:00+00:00",
    })
    rows = mysql_db.query("SELECT term FROM concept_cache WHERE cache_key = ?", ("abc123",))
    assert rows[0]["term"] == "consciousness"
