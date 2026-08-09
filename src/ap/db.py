"""Database connection and migration handling."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

DEFAULT_DSN = "postgresql://ap:ap@localhost:5432/ap"
SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def dsn() -> str:
    """Connection string, from DATABASE_URL if set."""
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


@contextmanager
def connect(autocommit: bool = False):
    """Yield a connection, committing on clean exit and rolling back on error."""
    conn = psycopg2.connect(dsn())
    conn.autocommit = autocommit
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def run_sql_file(conn, path: Path) -> None:
    """Execute a whole .sql file in one statement batch."""
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))


def migrate(conn) -> list[str]:
    """Apply schema and views. Both files are idempotent, so this is re-runnable."""
    applied = []
    for name in ("001_schema.sql", "002_views.sql"):
        run_sql_file(conn, SQL_DIR / name)
        applied.append(name)
    return applied


def run_rules(conn) -> dict[str, int]:
    """Run the rule engine and return a count of open exceptions per rule."""
    run_sql_file(conn, SQL_DIR / "003_rules.sql")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT rule_code, COUNT(*) AS n FROM exceptions "
            "WHERE resolved_at IS NULL GROUP BY rule_code ORDER BY rule_code"
        )
        return {row["rule_code"]: row["n"] for row in cur.fetchall()}


def file_hash(path: Path) -> str:
    """SHA-256 of a file, used to make ingestion idempotent per source file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def already_ingested(conn, hash_value: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ingest_runs WHERE file_hash = %s AND finished_at IS NOT NULL",
            (hash_value,),
        )
        return cur.fetchone() is not None


def query(conn, sql: str, params: tuple = ()) -> list[dict]:
    """Run a read query and return a list of dicts."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
