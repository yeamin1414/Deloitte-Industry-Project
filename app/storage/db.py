"""Operational data store.

SQLite keeps the project runnable anywhere. The repository layer is the only
module that issues SQL, so moving to Amazon Aurora PostgreSQL or Azure Database
for PostgreSQL only touches this file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    golden_id TEXT NOT NULL,
    enc_payload BLOB NOT NULL,
    site TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patient_index (
    token TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    PRIMARY KEY (token, patient_id)
);

CREATE TABLE IF NOT EXISTS match_reviews (
    id TEXT PRIMARY KEY,
    incoming_patient_id TEXT NOT NULL,
    candidate_patient_id TEXT NOT NULL,
    score REAL NOT NULL,
    band TEXT NOT NULL,
    evidence TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    message_control_id TEXT UNIQUE NOT NULL,
    patient_id TEXT NOT NULL,
    order_id TEXT,
    test_code TEXT NOT NULL,
    test_name TEXT NOT NULL,
    value REAL,
    units TEXT,
    reference_range TEXT,
    abnormal_flag TEXT,
    status TEXT NOT NULL,
    screening_label TEXT,
    screening_confidence REAL,
    instrument_serial TEXT,
    firmware_version TEXT,
    operator_id TEXT,
    reagent_batch TEXT,
    site TEXT,
    collected_at TEXT,
    resulted_at TEXT,
    received_at TEXT NOT NULL,
    delivered_at TEXT,
    turnaround_seconds REAL,
    event_route TEXT,
    rpr_hash TEXT,
    fhir TEXT
);
CREATE INDEX IF NOT EXISTS ix_results_batch ON results(reagent_batch);
CREATE INDEX IF NOT EXISTS ix_results_patient ON results(patient_id);

CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id TEXT NOT NULL,
    entry TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL,
    replicas TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    analyte TEXT NOT NULL,
    value REAL,
    units TEXT,
    rule TEXT NOT NULL,
    site TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    escalated_at TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    meta TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    provider TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keyring (
    provider TEXT PRIMARY KEY,
    wrapped_key BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS cost_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    units REAL NOT NULL,
    cost_usd REAL NOT NULL,
    site TEXT
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    # generic helpers -------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self.lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def insert(self, table: str, values: dict[str, Any]) -> None:
        cols = ", ".join(values)
        marks = ", ".join(f":{k}" for k in values)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values)

    def transaction(self):
        return _Tx(self)

    # audit -----------------------------------------------------------------
    def audit(self, actor: str, role: str, action: str, resource: str = "", detail: dict | None = None) -> None:
        self.insert(
            "audit_log",
            {
                "ts": utcnow(),
                "actor": actor,
                "role": role,
                "action": action,
                "resource": resource,
                "detail": json.dumps(detail or {}),
            },
        )

    def close(self) -> None:
        self._conn.close()


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db._conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
        return False
