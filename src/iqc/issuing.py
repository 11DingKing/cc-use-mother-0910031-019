"""生产领料：仅可领用已放行的可用库存，并发安全。"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .db import Database, add_event, one, rows
from .errors import InsufficientStockError, StateError
from .models import UnitStatus, utc_now_iso
from .store import change_occupancy, get_lot, get_part, next_unit_code
from .validation import optional_text, require_int, require_text


class IssuingService:
    """领料出库（FIFO，按库存单位粒度记录去向）。"""

    def __init__(self, db: Database, clock: Callable[[], str] = utc_now_iso):
        self._db = db
        self._now = clock

    def issue(self, *, part_no: str, qty: int, actor: str,
              lot_no: str | None = None, request_id: str | None = None) -> dict:
        """领料：从 AVAILABLE 库存单位按入库先后扣减。

        - 隔离中/检验中的批次不可见，天然阻止"复检完成前发料"。
        - 事务串行化 + 条件更新，并发领料不会超发。
        - request_id 相同的安全重试返回首次单据，不重复扣减。
        """
        part_no = require_text("part_no", part_no)
        qty = require_int("qty", qty, minimum=1)
        actor = require_text("actor", actor)
        lot_no = optional_text("lot_no", lot_no)
        request_id = optional_text("request_id", request_id)
        now = self._now()
        with self._db.transaction() as conn:
            if request_id is not None:
                existing = one(conn.execute(
                    "SELECT * FROM issues WHERE request_id = ?", (request_id,)))
                if existing is not None:
                    return self._payload(conn, existing, replayed=True)
            get_part(conn, part_no)
            if lot_no is not None:
                get_lot(conn, lot_no)
                units = rows(conn.execute(
                    "SELECT * FROM stock_units WHERE status = 'AVAILABLE'"
                    " AND part_no = ? AND lot_no = ? ORDER BY id",
                    (part_no, lot_no)))
            else:
                units = rows(conn.execute(
                    "SELECT * FROM stock_units WHERE status = 'AVAILABLE' AND part_no = ?"
                    " ORDER BY id",
                    (part_no,)))
            total = sum(u["qty"] for u in units)
            if total < qty:
                raise InsufficientStockError(
                    f"可用库存不足：需求 {qty}，可用 {total}",
                    details={"requested": qty, "available": total})
            try:
                issue_id = conn.execute(
                    "INSERT INTO issues (request_id, part_no, qty, requested_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (request_id, part_no, qty, actor, now)).lastrowid
            except sqlite3.IntegrityError:
                # 并发下同 request_id：返回先到的单据
                existing = one(conn.execute(
                    "SELECT * FROM issues WHERE request_id = ?", (request_id,)))
                if existing is None:
                    raise
                return self._payload(conn, existing, replayed=True)
            need = qty
            for unit in units:
                take = min(need, unit["qty"])
                if take == unit["qty"]:
                    cur = conn.execute(
                        "UPDATE stock_units SET status = 'ISSUED', updated_at = ?"
                        " WHERE id = ? AND status = 'AVAILABLE'",
                        (now, unit["id"]))
                    if cur.rowcount != 1:
                        raise StateError("库存已被并发变更，请重试")
                else:
                    cur = conn.execute(
                        "UPDATE stock_units SET qty = qty - ?, updated_at = ?"
                        " WHERE id = ? AND status = 'AVAILABLE' AND qty >= ?",
                        (take, now, unit["id"], take))
                    if cur.rowcount != 1:
                        raise StateError("库存已被并发变更，请重试")
                    # 拆出已领用子单位，保持“单位状态 × 数量”自洽
                    code = next_unit_code(conn, unit["lot_no"])
                    conn.execute(
                        "INSERT INTO stock_units (unit_code, lot_no, part_no, qty, status,"
                        " location_code, decision_id, parent_id, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, 'ISSUED', ?, ?, ?, ?, ?)",
                        (code, unit["lot_no"], unit["part_no"], take,
                         unit["location_code"], unit["decision_id"], unit["id"], now, now))
                change_occupancy(conn, unit["location_code"], -take)
                # 领料行始终指向来源单位，追溯“从哪个单位发出”
                conn.execute(
                    "INSERT INTO issue_lines (issue_id, unit_id, lot_no, qty)"
                    " VALUES (?, ?, ?, ?)",
                    (issue_id, unit["id"], unit["lot_no"], take))
                need -= take
                if need == 0:
                    break
            add_event(conn, now=now, lot_no=None, entity="issue", entity_id=issue_id,
                      action="ISSUE_CREATED", actor=actor,
                      detail={"part_no": part_no, "qty": qty, "lot_no": lot_no})
            issue = one(conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)))
            return self._payload(conn, issue, replayed=False)

    @staticmethod
    def _payload(conn: sqlite3.Connection, issue: dict, *, replayed: bool) -> dict:
        lines = rows(conn.execute(
            "SELECT il.issue_id, il.unit_id, su.unit_code, il.lot_no, il.qty"
            " FROM issue_lines il JOIN stock_units su ON su.id = il.unit_id"
            " WHERE il.issue_id = ? ORDER BY il.id",
            (issue["id"],)))
        return {"issue": issue, "lines": lines, "replayed": replayed}
