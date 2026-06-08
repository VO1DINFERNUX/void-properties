"""SQLite connection helper for void-properties."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "void_properties.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the table's initial release. SQLite's ALTER TABLE has
# no `ADD COLUMN IF NOT EXISTS` (and a plain `ADD COLUMN` in schema.sql would
# make `executescript` fail on every re-run once the column exists), so
# `init_db()` adds these itself, guarded by a check against `PRAGMA table_info`.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "leads": [
        ("deal_score", "INTEGER CHECK (deal_score BETWEEN 1 AND 10)"),
        ("deal_score_rationale", "TEXT"),
    ],
    "buyers": [
        ("buyer_phone", "TEXT"),
        ("buyer_email", "TEXT"),
    ],
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db(db_path: Path = DB_PATH) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection(db_path) as conn:
        conn.executescript(schema)
        _ensure_columns(conn)
