"""SQLite 存储：表结构、连接工厂与事务边界。

所有写操作都必须放在 ``Database.transaction()`` 里（BEGIN IMMEDIATE），
保证多步写入要么全部提交、要么整体回滚，不会留下半上架状态。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    part_no TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    spec    TEXT
);

CREATE TABLE IF NOT EXISTS locations (
    code     TEXT PRIMARY KEY,
    kind     TEXT NOT NULL CHECK (kind IN ('QUARANTINE', 'AVAILABLE')),
    capacity INTEGER CHECK (capacity IS NULL OR capacity > 0),
    occupied INTEGER NOT NULL DEFAULT 0 CHECK (occupied >= 0),
    CHECK (capacity IS NULL OR occupied <= capacity)
);

CREATE TABLE IF NOT EXISTS lots (
    lot_no        TEXT PRIMARY KEY,
    part_no       TEXT NOT NULL REFERENCES parts (part_no),
    supplier      TEXT NOT NULL,
    po_ref        TEXT,
    qty_received  INTEGER NOT NULL CHECK (qty_received > 0),
    status        TEXT NOT NULL CHECK (status IN
        ('RECEIVED', 'INSPECTING', 'AWAITING_DISPOSITION', 'PARTIALLY_RELEASED',
         'RELEASED', 'RETURNED', 'CLOSED')),
    inspect_round INTEGER NOT NULL DEFAULT 0,
    received_by   TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sampling_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no      TEXT NOT NULL REFERENCES lots (lot_no),
    round       INTEGER NOT NULL CHECK (round > 0),
    standard    TEXT NOT NULL,
    level       TEXT NOT NULL,
    aql         TEXT NOT NULL,
    sample_size INTEGER NOT NULL CHECK (sample_size > 0),
    accept_qty  INTEGER NOT NULL CHECK (accept_qty >= 0),
    reject_qty  INTEGER NOT NULL CHECK (reject_qty > 0),
    status      TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'COMPLETED')),
    result      TEXT CHECK (result IN ('PASS', 'FAIL')),
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    completed_by TEXT,
    completed_at TEXT,
    UNIQUE (lot_no, round)
);

CREATE TABLE IF NOT EXISTS inspection_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL REFERENCES sampling_plans (id),
    seq           INTEGER NOT NULL CHECK (seq > 0),
    name          TEXT NOT NULL,
    requirement   TEXT NOT NULL,
    result        TEXT CHECK (result IN ('PASS', 'FAIL')),
    defective_qty INTEGER NOT NULL DEFAULT 0 CHECK (defective_qty >= 0),
    measured      TEXT,
    inspected_by  TEXT,
    inspected_at  TEXT,
    UNIQUE (plan_id, seq)
);

CREATE TABLE IF NOT EXISTS defects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no       TEXT NOT NULL REFERENCES lots (lot_no),
    item_id      INTEGER REFERENCES inspection_items (id),
    defect_code  TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('CRITICAL', 'MAJOR', 'MINOR')),
    qty          INTEGER NOT NULL CHECK (qty > 0),
    description  TEXT NOT NULL,
    evidence_uri TEXT,
    recorded_by  TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no       TEXT NOT NULL REFERENCES lots (lot_no),
    qty          INTEGER NOT NULL CHECK (qty > 0),
    consumed_qty INTEGER NOT NULL DEFAULT 0 CHECK (consumed_qty >= 0),
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CONSUMED')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_by   TEXT,
    decided_at   TEXT,
    decision_note TEXT,
    CHECK (consumed_qty <= qty)
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no        TEXT NOT NULL REFERENCES lots (lot_no),
    round         INTEGER NOT NULL CHECK (round >= 0),
    dtype         TEXT NOT NULL CHECK (dtype IN
        ('RELEASE', 'PARTIAL_RELEASE', 'CONCESSION_RELEASE', 'RETURN', 'REINSPECT')),
    qty           INTEGER NOT NULL CHECK (qty > 0),
    reason        TEXT NOT NULL,
    concession_id INTEGER REFERENCES concessions (id),
    request_id    TEXT,
    decided_by    TEXT NOT NULL,
    decided_at    TEXT NOT NULL,
    UNIQUE (lot_no, request_id)
);

CREATE TABLE IF NOT EXISTS stock_units (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_code     TEXT NOT NULL UNIQUE,
    lot_no        TEXT NOT NULL REFERENCES lots (lot_no),
    part_no       TEXT NOT NULL REFERENCES parts (part_no),
    qty           INTEGER NOT NULL CHECK (qty > 0),
    status        TEXT NOT NULL CHECK (status IN ('QUARANTINED', 'AVAILABLE', 'ISSUED', 'RETURNED')),
    location_code TEXT NOT NULL REFERENCES locations (code),
    decision_id   INTEGER REFERENCES decisions (id),
    parent_id     INTEGER REFERENCES stock_units (id),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_lot ON stock_units (lot_no, status);
CREATE INDEX IF NOT EXISTS idx_units_part ON stock_units (part_no, status);

CREATE TABLE IF NOT EXISTS issues (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT UNIQUE,
    part_no      TEXT NOT NULL REFERENCES parts (part_no),
    qty          INTEGER NOT NULL CHECK (qty > 0),
    requested_by TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_lines (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues (id),
    unit_id  INTEGER NOT NULL REFERENCES stock_units (id),
    lot_no   TEXT NOT NULL REFERENCES lots (lot_no),
    qty      INTEGER NOT NULL CHECK (qty > 0)
);
CREATE INDEX IF NOT EXISTS idx_issue_lines_unit ON issue_lines (unit_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no     TEXT,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    action     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_lot ON events (lot_no, id);
"""


def one(cursor: sqlite3.Cursor) -> dict | None:
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


def add_event(conn: sqlite3.Connection, *, now: str, lot_no: str | None, entity: str,
              entity_id: object, action: str, actor: str, detail: dict | None = None) -> None:
    """在同一事务内追加审计事件，供批次追溯使用。"""
    conn.execute(
        "INSERT INTO events (lot_no, entity, entity_id, action, actor, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lot_no, entity, str(entity_id), action, actor,
         json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), now),
    )


class Database:
    """连接工厂与事务边界。

    每个事务使用独立连接，因此可以在多线程（HTTP 服务、并发领料）下安全共享。
    """

    def __init__(self, path: str | Path = ":memory:"):
        self._anchor: sqlite3.Connection | None = None
        if str(path) == ":memory:":
            # 共享缓存内存库：需要常驻连接防止库被回收
            self._dsn = f"file:iqc-{id(self)}?mode=memory&cache=shared"
            self._uri = True
            self._anchor = self._connect()
        else:
            self._dsn = str(path)
            self._uri = False
            Path(self._dsn).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode = WAL")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._dsn, uri=self._uri, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 串行化写者，异常时整体回滚。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """只读连接（自动提交模式）。"""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None
