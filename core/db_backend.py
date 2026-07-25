"""
Database Backend
----------------
Dialect handling for the two backends SEEKER runs on:

  sqlite  — default for local CLI use and tests. Zero setup.
  mysql   — the multi-user Docker deployment.

Everything above this module (core/database.py and its callers) is written
once against the generic helpers; only the SQL dialect differs here.

Selection, in order:
  1. SEEKER_DB_BACKEND=sqlite|mysql
  2. mysql, if MYSQL_HOST or MYSQL_URL is set
  3. sqlite

Note: db/conceptnet.db stays SQLite regardless — it is a read-only reference
corpus, not pipeline state.
"""

import os
import logging
import threading
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type vocabulary
#
# The schema is written once using these tokens and rendered per dialect.
# MySQL cannot index TEXT without a prefix length, so anything that is a
# primary key, part of a unique constraint, or indexed must be VARCHAR.
# 191 keeps utf8mb4 indexes inside InnoDB's 767-byte legacy limit.
# ---------------------------------------------------------------------------

_TYPES = {
    "sqlite": {
        "{ID}":       "TEXT",
        "{KEY}":      "TEXT",
        "{TEXT}":     "TEXT",
        "{LONGTEXT}": "TEXT",
        "{INT}":      "INTEGER",
        "{REAL}":     "REAL",
    },
    "mysql": {
        "{ID}":       "VARCHAR(64)",
        "{KEY}":      "VARCHAR(191)",
        "{TEXT}":     "TEXT",
        "{LONGTEXT}": "LONGTEXT",
        "{INT}":      "INT",
        "{REAL}":     "DOUBLE",
    },
}


def render_schema(schema: str, dialect: str) -> str:
    """Substitute the type tokens for one dialect."""
    out = schema
    for token, sql_type in _TYPES[dialect].items():
        out = out.replace(token, sql_type)
    return out


def split_statements(sql: str) -> list[str]:
    """Split a DDL script into individual statements, dropping comments."""
    statements = []
    for raw in sql.split(";"):
        lines = [
            line for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    return statements


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class Backend:
    name = ""
    placeholder = "?"

    def connect(self):
        raise NotImplementedError

    def upsert_sql(self, table: str, columns: list[str]) -> str:
        raise NotImplementedError

    def init_schema(self, schema: str) -> None:
        raise NotImplementedError


class SQLiteBackend(Backend):
    name = "sqlite"
    placeholder = "?"

    def __init__(self, path_getter):
        self._path_getter = path_getter

    @property
    def path(self):
        return self._path_getter()

    def connect(self):
        import sqlite3
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def upsert_sql(self, table: str, columns: list[str]) -> str:
        cols = ", ".join(columns)
        marks = ", ".join(["?"] * len(columns))
        return f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})"

    def init_schema(self, schema: str) -> None:
        with connection() as conn:
            conn.executescript(render_schema(schema, "sqlite"))


class MySQLBackend(Backend):
    name = "mysql"
    placeholder = "%s"

    def __init__(self):
        self._local = threading.local()
        self._settings = _mysql_settings()

    def _driver(self):
        try:
            import pymysql
            import pymysql.cursors
            return pymysql
        except ImportError as e:
            raise RuntimeError(
                "MySQL backend selected but PyMySQL is not installed. "
                "Install it with: pip install 'PyMySQL>=1.1.0'"
            ) from e

    def connect(self):
        """
        One connection per thread, pinged and reconnected as needed. Cheaper
        than dialing per query, and adequate for a web process plus a worker.
        """
        pymysql = self._driver()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.ping()          # raises if the connection has gone away
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None

        conn = pymysql.connect(
            host=self._settings["host"],
            port=self._settings["port"],
            user=self._settings["user"],
            password=self._settings["password"],
            database=self._settings["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
        self._local.conn = conn
        return conn

    def upsert_sql(self, table: str, columns: list[str]) -> str:
        cols = ", ".join(columns)
        marks = ", ".join(["%s"] * len(columns))
        updates = ", ".join(f"{c} = VALUES({c})" for c in columns)
        return (f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
                f"ON DUPLICATE KEY UPDATE {updates}")

    def init_schema(self, schema: str) -> None:
        # MySQL has no CREATE INDEX IF NOT EXISTS, so duplicate-index errors
        # are expected on re-init and are swallowed.
        rendered = render_schema(schema, "mysql")
        with connection() as conn:
            with conn.cursor() as cur:
                for stmt in split_statements(rendered):
                    if stmt.upper().startswith("CREATE INDEX"):
                        stmt = stmt.replace("IF NOT EXISTS ", "", 1)
                        try:
                            cur.execute(stmt)
                        except Exception as e:
                            if "duplicate key name" not in str(e).lower():
                                raise
                    else:
                        cur.execute(stmt)


def _mysql_settings() -> dict:
    """Read MySQL connection settings from MYSQL_URL or discrete MYSQL_* vars."""
    url = os.environ.get("MYSQL_URL", "").strip()
    if url:
        parsed = urlparse(url)
        return {
            "host":     parsed.hostname or "localhost",
            "port":     parsed.port or 3306,
            "user":     parsed.username or "seeker",
            "password": parsed.password or "",
            "database": (parsed.path or "/seeker").lstrip("/") or "seeker",
        }
    return {
        "host":     os.environ.get("MYSQL_HOST", "localhost"),
        "port":     int(os.environ.get("MYSQL_PORT", "3306")),
        "user":     os.environ.get("MYSQL_USER", "seeker"),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DATABASE", "seeker"),
    }


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_backend: Optional[Backend] = None
_backend_lock = threading.Lock()


def _detect_backend_name() -> str:
    explicit = os.environ.get("SEEKER_DB_BACKEND", "").strip().lower()
    if explicit in ("sqlite", "mysql"):
        return explicit
    if os.environ.get("MYSQL_HOST") or os.environ.get("MYSQL_URL"):
        return "mysql"
    return "sqlite"


def get_backend() -> Backend:
    global _backend
    with _backend_lock:
        if _backend is None:
            name = _detect_backend_name()
            if name == "mysql":
                _backend = MySQLBackend()
                logger.info(
                    f"[DB] mysql backend — {_backend._settings['user']}@"
                    f"{_backend._settings['host']}:{_backend._settings['port']}"
                    f"/{_backend._settings['database']}"
                )
            else:
                from core import database
                _backend = SQLiteBackend(lambda: database.DB_PATH)
                logger.info(f"[DB] sqlite backend — {_backend.path}")
        return _backend


def reset_backend() -> None:
    """Drop the cached backend so the next call re-reads the environment."""
    global _backend
    with _backend_lock:
        _backend = None


def placeholder() -> str:
    return get_backend().placeholder


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@contextmanager
def connection():
    """
    Yield a connection, committing on success and rolling back on error.

    SQLite connections are closed on exit; MySQL connections are thread-local
    and stay open for reuse.
    """
    backend = get_backend()
    conn = backend.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if backend.name == "sqlite":
            conn.close()


@contextmanager
def cursor():
    """Yield a cursor returning dict-like rows on both backends."""
    with connection() as conn:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            try:
                cur.close()
            except Exception:
                pass


def rows_to_dicts(rows) -> list[dict]:
    """Normalise sqlite3.Row and PyMySQL DictCursor output to plain dicts."""
    return [dict(r) for r in (rows or [])]
