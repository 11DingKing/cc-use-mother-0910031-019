"""只读查询：批次详情、库存汇总与全链路追溯。"""
from __future__ import annotations

from .db import Database, one, rows
from .errors import NotFoundError
from .models import UnitStatus
from .store import get_lot
from .validation import require_int, require_text


class QueryService:
    def __init__(self, db: Database):
        self._db = db

    def get_lot(self, lot_no: str) -> dict:
        """批次详情：基本信息 + 各状态库存数量 + 最近一次检验结论。"""
        lot_no = require_text("lot_no", lot_no)
        with self._db.read() as conn:
            lot = get_lot(conn, lot_no)
            lot["stock"] = self._stock_by_status(conn, lot_no)
            lot["latest_plan"] = one(conn.execute(
                "SELECT id, round, status, result FROM sampling_plans"
                " WHERE lot_no = ? ORDER BY round DESC LIMIT 1",
                (lot_no,)))
            return lot

    def list_lots(self, *, status: str | None = None, part_no: str | None = None) -> list[dict]:
        sql = "SELECT * FROM lots"
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if part_no:
            clauses.append("part_no = ?")
            params.append(part_no)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY received_at, lot_no"
        with self._db.read() as conn:
            return rows(conn.execute(sql, params))

    def list_units(self, lot_no: str) -> list[dict]:
        lot_no = require_text("lot_no", lot_no)
        with self._db.read() as conn:
            get_lot(conn, lot_no)
            return rows(conn.execute(
                "SELECT * FROM stock_units WHERE lot_no = ? ORDER BY id", (lot_no,)))

    def get_concession(self, concession_id: int) -> dict:
        concession_id = require_int("concession_id", concession_id, minimum=1)
        with self._db.read() as conn:
            concession = one(conn.execute(
                "SELECT * FROM concessions WHERE id = ?", (concession_id,)))
            if concession is None:
                raise NotFoundError(f"让步审批不存在：{concession_id}")
            return concession

    def stock_summary(self, *, part_no: str | None = None) -> list[dict]:
        """按零部件汇总各状态库存数量。"""
        sql = "SELECT part_no, status, SUM(qty) AS qty FROM stock_units"
        params: list = []
        if part_no:
            sql += " WHERE part_no = ?"
            params.append(part_no)
        sql += " GROUP BY part_no, status ORDER BY part_no"
        summary: dict[str, dict] = {}
        with self._db.read() as conn:
            for row in rows(conn.execute(sql, params)):
                bucket = summary.setdefault(row["part_no"], {
                    "part_no": row["part_no"],
                    UnitStatus.QUARANTINED: 0, UnitStatus.AVAILABLE: 0,
                    UnitStatus.ISSUED: 0, UnitStatus.RETURNED: 0})
                bucket[row["status"]] = row["qty"]
        return list(summary.values())

    def trace_lot(self, lot_no: str) -> dict:
        """沿批次追溯：每个库存单位的当前状态及其决定依据（决定/让步/领料去向）。"""
        lot_no = require_text("lot_no", lot_no)
        with self._db.read() as conn:
            lot = get_lot(conn, lot_no)
            lot["stock"] = self._stock_by_status(conn, lot_no)
            plans = rows(conn.execute(
                "SELECT * FROM sampling_plans WHERE lot_no = ? ORDER BY round", (lot_no,)))
            for plan in plans:
                items = rows(conn.execute(
                    "SELECT * FROM inspection_items WHERE plan_id = ? ORDER BY seq",
                    (plan["id"],)))
                for item in items:
                    item["defects"] = rows(conn.execute(
                        "SELECT * FROM defects WHERE item_id = ? ORDER BY id", (item["id"],)))
                plan["items"] = items
            lot_defects = rows(conn.execute(
                "SELECT * FROM defects WHERE lot_no = ? AND item_id IS NULL ORDER BY id",
                (lot_no,)))
            decisions = rows(conn.execute(
                "SELECT * FROM decisions WHERE lot_no = ? ORDER BY id", (lot_no,)))
            concessions = rows(conn.execute(
                "SELECT * FROM concessions WHERE lot_no = ? ORDER BY id", (lot_no,)))
            units = rows(conn.execute(
                "SELECT * FROM stock_units WHERE lot_no = ? ORDER BY id", (lot_no,)))
            for unit in units:
                decision = None
                if unit["decision_id"] is not None:
                    decision = one(conn.execute(
                        "SELECT * FROM decisions WHERE id = ?", (unit["decision_id"],)))
                unit["decision"] = decision
                if decision and decision["concession_id"] is not None:
                    unit["concession"] = one(conn.execute(
                        "SELECT * FROM concessions WHERE id = ?",
                        (decision["concession_id"],)))
                else:
                    unit["concession"] = None
                unit["issues"] = rows(conn.execute(
                    "SELECT il.issue_id, il.qty, i.part_no, i.requested_by, i.created_at"
                    " FROM issue_lines il JOIN issues i ON i.id = il.issue_id"
                    " WHERE il.unit_id = ? ORDER BY il.id",
                    (unit["id"],)))
            events = rows(conn.execute(
                "SELECT * FROM events WHERE lot_no = ? ORDER BY id", (lot_no,)))
            return {"lot": lot, "plans": plans, "lot_defects": lot_defects,
                    "decisions": decisions, "concessions": concessions,
                    "units": units, "events": events}

    @staticmethod
    def _stock_by_status(conn, lot_no: str) -> dict:
        stock = {status: 0 for status in UnitStatus.ALL}
        for row in rows(conn.execute(
                "SELECT status, SUM(qty) AS qty FROM stock_units WHERE lot_no = ? GROUP BY status",
                (lot_no,))):
            stock[row["status"]] = row["qty"]
        return stock
