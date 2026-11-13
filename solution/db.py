"""Database connection, schema setup and key verification."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .pii import PiiCodec, load_codec
from .schemas import Disposition

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
SCHEMA_VERSION = "1"


class KeyMismatchError(RuntimeError):
    pass


def default_key_file(db_path: Path) -> Path:
    # '<db>.key' sits next to the DB for local runs. In production the key
    # comes from AU_PII_KEY (injected from a secrets manager / KMS) instead.
    return db_path.with_name(db_path.name + ".key")


def connect(db_path: Path, key_file: Path | None = None) -> tuple[sqlite3.Connection, PiiCodec]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size = -131072")  # 128 MiB
    conn.executescript(SCHEMA_SQL)

    codec = load_codec(key_file or default_key_file(db_path))
    _check_meta(conn, codec)
    return conn, codec


def _check_meta(conn: sqlite3.Connection, codec: PiiCodec) -> None:
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if not meta:
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                         [("schema_version", SCHEMA_VERSION), ("key_fingerprint", codec.fingerprint)])
        return
    if meta.get("key_fingerprint") != codec.fingerprint:
        raise KeyMismatchError(
            "PII key does not match the one this database was created with; "
            "phone matching would silently break. Set AU_PII_KEY / --key-file correctly.")


def sync_dispositions(conn: sqlite3.Connection, dispositions: list[Disposition]) -> None:
    conn.execute("BEGIN")
    conn.execute("DELETE FROM disposition")
    conn.executemany(
        "INSERT INTO disposition (workflow_id, code, label, is_terminal, is_success) VALUES (?,?,?,?,?)",
        [(d.workflow_id, d.code, d.label, int(d.is_terminal), int(d.is_success)) for d in dispositions])
    conn.execute("COMMIT")
