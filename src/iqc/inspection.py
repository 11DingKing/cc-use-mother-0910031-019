"""抽样检验：抽样计划、检验项结果与缺陷证据。"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .db import Database, add_event, one
from .errors import NotFoundError, StateError, ValidationError
from .models import ItemResult, LotStatus, PlanStatus, Severity, utc_now_iso
from .store import get_lot, quarantined_qty
from .validation import optional_text, require_int, require_text


def _normalize_items(items: object) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise ValidationError("items 必须是非空列表")
    specs = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValidationError("items 元素必须是对象")
        specs.append({"name": require_text("items[].name", raw.get("name")),
                      "requirement": require_text("items[].requirement", raw.get("requirement"))})
    return specs


def _normalize_defect(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("defects 元素必须是对象")
    severity = require_text("defects[].severity", raw.get("severity"))
    if severity not in Severity.ALL:
        raise ValidationError(f"severity 必须是 {'/'.join(Severity.ALL)}")
    return {
        "defect_code": require_text("defects[].defect_code", raw.get("defect_code")),
        "severity": severity,
        "qty": require_int("defects[].qty", raw.get("qty"), minimum=1),
        "description": require_text("defects[].description", raw.get("description")),
        "evidence_uri": optional_text("defects[].evidence_uri", raw.get("evidence_uri")),
    }


class InspectionService:
    """抽样计划、检验项与缺陷证据的录入和判定。"""

    def __init__(self, db: Database, clock: Callable[[], str] = utc_now_iso):
        self._db = db
        self._now = clock

    def create_plan(self, *, lot_no: str, actor: str, standard: str, level: str, aql: str,
                    sample_size: int, accept_qty: int, reject_qty: int,
                    items: list[dict]) -> dict:
        """创建抽样计划并开始检验（批次 RECEIVED → INSPECTING，复检时轮次 +1）。"""
        lot_no = require_text("lot_no", lot_no)
        actor = require_text("actor", actor)
        standard = require_text("standard", standard)
        level = require_text("level", level)
        aql = require_text("aql", aql)
        sample_size = require_int("sample_size", sample_size, minimum=1)
        accept_qty = require_int("accept_qty", accept_qty, minimum=0)
        reject_qty = require_int("reject_qty", reject_qty, minimum=1)
        if reject_qty != accept_qty + 1:
            raise ValidationError("拒收数 Re 必须等于接收数 Ac + 1")
        item_specs = _normalize_items(items)
        now = self._now()
        with self._db.transaction() as conn:
            lot = get_lot(conn, lot_no)
            if lot["status"] == LotStatus.RECEIVED:
                round_no = 1
            elif lot["status"] == LotStatus.INSPECTING:
                open_plan = one(conn.execute(
                    "SELECT id FROM sampling_plans WHERE lot_no = ? AND status = 'OPEN'", (lot_no,)))
                if open_plan is not None:
                    raise StateError("当前抽样计划尚未完成")
                round_no = lot["inspect_round"] + 1
            else:
                raise StateError(f"批次状态 {lot['status']} 不允许创建抽样计划")
            remaining = quarantined_qty(conn, lot_no)
            if sample_size > remaining:
                raise ValidationError(f"抽样数量 {sample_size} 超过在检数量 {remaining}")
            plan_id = conn.execute(
                "INSERT INTO sampling_plans (lot_no, round, standard, level, aql, sample_size,"
                " accept_qty, reject_qty, status, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (lot_no, round_no, standard, level, aql, sample_size, accept_qty,
                 reject_qty, PlanStatus.OPEN, actor, now)).lastrowid
            item_rows = []
            for seq, spec in enumerate(item_specs, start=1):
                item_id = conn.execute(
                    "INSERT INTO inspection_items (plan_id, seq, name, requirement) VALUES (?, ?, ?, ?)",
                    (plan_id, seq, spec["name"], spec["requirement"])).lastrowid
                item_rows.append({"id": item_id, "seq": seq, "name": spec["name"],
                                  "requirement": spec["requirement"], "result": None,
                                  "defective_qty": 0})
            cur = conn.execute(
                "UPDATE lots SET status = ?, inspect_round = ?, updated_at = ?"
                " WHERE lot_no = ? AND status IN (?, ?)",
                (LotStatus.INSPECTING, round_no, now, lot_no,
                 LotStatus.RECEIVED, LotStatus.INSPECTING))
            if cur.rowcount != 1:
                raise StateError("批次状态已变化，请重试")
            add_event(conn, now=now, lot_no=lot_no, entity="sampling_plan", entity_id=plan_id,
                      action="PLAN_CREATED", actor=actor,
                      detail={"round": round_no, "sample_size": sample_size,
                              "accept_qty": accept_qty, "reject_qty": reject_qty})
            return {"id": plan_id, "lot_no": lot_no, "round": round_no, "standard": standard,
                    "level": level, "aql": aql, "sample_size": sample_size,
                    "accept_qty": accept_qty, "reject_qty": reject_qty,
                    "status": PlanStatus.OPEN, "created_by": actor, "created_at": now,
                    "items": item_rows}

    def record_item_result(self, *, lot_no: str, item_id: int, result: str, actor: str,
                           defective_qty: int = 0, measured: str | None = None,
                           defects: list[dict] | None = None) -> dict:
        """录入单个检验项结果，可随附缺陷证据（同事务写入）。"""
        lot_no = require_text("lot_no", lot_no)
        item_id = require_int("item_id", item_id, minimum=1)
        actor = require_text("actor", actor)
        if result not in ItemResult.ALL:
            raise ValidationError("result 必须是 PASS 或 FAIL")
        defective_qty = require_int("defective_qty", defective_qty, minimum=0)
        if result == ItemResult.FAIL and defective_qty < 1:
            raise ValidationError("不合格检验项必须给出不合格数量")
        if result == ItemResult.PASS and defective_qty != 0:
            raise ValidationError("合格检验项的不合格数量必须为 0")
        defect_specs = [_normalize_defect(d) for d in (defects or [])]
        now = self._now()
        with self._db.transaction() as conn:
            lot = get_lot(conn, lot_no)
            item = one(conn.execute(
                "SELECT ii.*, sp.status AS plan_status FROM inspection_items ii"
                " JOIN sampling_plans sp ON sp.id = ii.plan_id"
                " WHERE ii.id = ? AND sp.lot_no = ?",
                (item_id, lot_no)))
            if item is None:
                raise NotFoundError(f"检验项不存在：{item_id}")
            if item["plan_status"] != PlanStatus.OPEN:
                raise StateError("抽样计划已关闭，不能录入结果")
            if lot["status"] != LotStatus.INSPECTING:
                raise StateError(f"批次状态 {lot['status']} 不允许录入检验结果")
            if item["result"] is not None:
                raise StateError("检验项已记录结果")
            conn.execute(
                "UPDATE inspection_items SET result = ?, defective_qty = ?, measured = ?,"
                " inspected_by = ?, inspected_at = ? WHERE id = ?",
                (result, defective_qty, measured, actor, now, item_id))
            defect_rows = [self._insert_defect(conn, lot_no=lot_no, item_id=item_id,
                                               spec=spec, actor=actor, now=now)
                           for spec in defect_specs]
            add_event(conn, now=now, lot_no=lot_no, entity="inspection_item", entity_id=item_id,
                      action="ITEM_RECORDED", actor=actor,
                      detail={"result": result, "defective_qty": defective_qty})
            updated = one(conn.execute("SELECT * FROM inspection_items WHERE id = ?", (item_id,)))
            updated["defects"] = defect_rows
            return updated

    def add_defect(self, *, lot_no: str, defect_code: str, severity: str, qty: int,
                   description: str, actor: str, item_id: int | None = None,
                   evidence_uri: str | None = None) -> dict:
        """单独追加缺陷证据（检验中或待判定阶段均可）。"""
        spec = _normalize_defect({"defect_code": defect_code, "severity": severity, "qty": qty,
                                  "description": description, "evidence_uri": evidence_uri})
        lot_no = require_text("lot_no", lot_no)
        actor = require_text("actor", actor)
        now = self._now()
        with self._db.transaction() as conn:
            lot = get_lot(conn, lot_no)
            if lot["status"] not in (LotStatus.INSPECTING, LotStatus.AWAITING_DISPOSITION):
                raise StateError(f"批次状态 {lot['status']} 不允许登记缺陷")
            if item_id is not None:
                item_id = require_int("item_id", item_id, minimum=1)
                item = one(conn.execute(
                    "SELECT ii.id FROM inspection_items ii"
                    " JOIN sampling_plans sp ON sp.id = ii.plan_id"
                    " WHERE ii.id = ? AND sp.lot_no = ?",
                    (item_id, lot_no)))
                if item is None:
                    raise NotFoundError(f"检验项不存在：{item_id}")
            row = self._insert_defect(conn, lot_no=lot_no, item_id=item_id,
                                      spec=spec, actor=actor, now=now)
            add_event(conn, now=now, lot_no=lot_no, entity="defect", entity_id=row["id"],
                      action="DEFECT_RECORDED", actor=actor,
                      detail={"defect_code": spec["defect_code"], "severity": spec["severity"],
                              "qty": spec["qty"], "item_id": item_id})
            return row

    def complete_inspection(self, *, lot_no: str, actor: str) -> dict:
        """完成检验：按 Ac/Re 判定批次，批次 INSPECTING → AWAITING_DISPOSITION。"""
        lot_no = require_text("lot_no", lot_no)
        actor = require_text("actor", actor)
        now = self._now()
        with self._db.transaction() as conn:
            lot = get_lot(conn, lot_no)
            if lot["status"] != LotStatus.INSPECTING:
                raise StateError(f"批次状态 {lot['status']} 不允许完成检验")
            plan = one(conn.execute(
                "SELECT * FROM sampling_plans WHERE lot_no = ? AND status = 'OPEN'"
                " ORDER BY round DESC LIMIT 1",
                (lot_no,)))
            if plan is None:
                raise StateError("没有进行中的抽样计划")
            pending = one(conn.execute(
                "SELECT COUNT(*) AS c FROM inspection_items WHERE plan_id = ? AND result IS NULL",
                (plan["id"],)))["c"]
            if pending:
                raise StateError(f"还有 {pending} 个检验项未记录结果")
            total = one(conn.execute(
                "SELECT COALESCE(SUM(defective_qty), 0) AS t FROM inspection_items WHERE plan_id = ?",
                (plan["id"],)))["t"]
            verdict = ItemResult.PASS if total <= plan["accept_qty"] else ItemResult.FAIL
            cur = conn.execute(
                "UPDATE sampling_plans SET status = ?, result = ?, completed_by = ?, completed_at = ?"
                " WHERE id = ? AND status = 'OPEN'",
                (PlanStatus.COMPLETED, verdict, actor, now, plan["id"]))
            if cur.rowcount != 1:
                raise StateError("抽样计划状态已变化，请重试")
            cur = conn.execute(
                "UPDATE lots SET status = ?, updated_at = ? WHERE lot_no = ? AND status = ?",
                (LotStatus.AWAITING_DISPOSITION, now, lot_no, LotStatus.INSPECTING))
            if cur.rowcount != 1:
                raise StateError("批次状态已变化，请重试")
            add_event(conn, now=now, lot_no=lot_no, entity="sampling_plan", entity_id=plan["id"],
                      action="INSPECTION_COMPLETED", actor=actor,
                      detail={"round": plan["round"], "verdict": verdict,
                              "total_defective": total, "accept_qty": plan["accept_qty"],
                              "reject_qty": plan["reject_qty"]})
            return {"plan_id": plan["id"], "round": plan["round"], "result": verdict,
                    "total_defective": total, "accept_qty": plan["accept_qty"],
                    "reject_qty": plan["reject_qty"], "lot_status": LotStatus.AWAITING_DISPOSITION}

    @staticmethod
    def _insert_defect(conn: sqlite3.Connection, *, lot_no: str, item_id: int | None,
                       spec: dict, actor: str, now: str) -> dict:
        defect_id = conn.execute(
            "INSERT INTO defects (lot_no, item_id, defect_code, severity, qty, description,"
            " evidence_uri, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lot_no, item_id, spec["defect_code"], spec["severity"], spec["qty"],
             spec["description"], spec["evidence_uri"], actor, now)).lastrowid
        return one(conn.execute("SELECT * FROM defects WHERE id = ?", (defect_id,)))
