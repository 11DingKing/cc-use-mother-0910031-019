"""处置决定：放行（含部分放行/让步）、退货、复检与让步审批。

放行在同一事务内完成：验收决定落库 → 隔离库存转移/拆分 → 库位占用更新 →
批次状态迁移 → 审计事件。任一步失败整体回滚，不会留下半上架状态。
"""
from __future__ import annotations

from collections.abc import Callable

from .db import Database, add_event, one
from .errors import NotFoundError, StateError, ValidationError
from .models import (ConcessionStatus, DecisionType, ItemResult, LocationKind,
                     LotStatus, UnitStatus, utc_now_iso)
from .store import (change_occupancy, get_location, get_lot, quarantined_qty,
                    take_units)
from .validation import optional_text, require_int, require_text

_DISPOSABLE = (LotStatus.AWAITING_DISPOSITION, LotStatus.PARTIALLY_RELEASED)


class DispositionService:
    """批次处置与让步审批。"""

    def __init__(self, db: Database, clock: Callable[[], str] = utc_now_iso):
        self._db = db
        self._now = clock

    # ------------------------------------------------------------------ 放行
    def release(self, *, lot_no: str, actor: str, reason: str, target_location: str,
                qty: int | None = None, concession_id: int | None = None,
                request_id: str | None = None) -> dict:
        """放行：原子更新验收决定与可用库存。

        - 检验合格：直接放行（qty 小于待处置数量时为部分放行）。
        - 检验不合格：必须携带已批准的让步审批，且数量不超过审批剩余量。
        - request_id 相同的安全重试直接返回首次决定，不重复上架。
        """
        lot_no = require_text("lot_no", lot_no)
        actor = require_text("actor", actor)
        reason = require_text("reason", reason)
        target_location = require_text("target_location", target_location)
        request_id = optional_text("request_id", request_id)
        now = self._now()
        with self._db.transaction() as conn:
            if request_id is not None:
                existing = one(conn.execute(
                    "SELECT * FROM decisions WHERE lot_no = ? AND request_id = ?",
                    (lot_no, request_id)))
                if existing is not None:
                    return {"replayed": True, "decision": existing,
                            "lot": get_lot(conn, lot_no), "units_moved": []}
            lot = get_lot(conn, lot_no)
            if lot["status"] not in _DISPOSABLE:
                raise StateError(f"批次状态 {lot['status']} 不允许放行")
            target = get_location(conn, target_location)
            if target["kind"] != LocationKind.AVAILABLE:
                raise ValidationError("放行目标必须是良品库位")
            remaining = quarantined_qty(conn, lot_no)
            take = remaining if qty is None else require_int("qty", qty, minimum=1)
            if take > remaining:
                raise ValidationError(f"放行数量 {take} 超过待处置数量 {remaining}",
                                      details={"remaining": remaining})
            plan = one(conn.execute(
                "SELECT * FROM sampling_plans WHERE lot_no = ? AND status = 'COMPLETED'"
                " ORDER BY round DESC LIMIT 1",
                (lot_no,)))
            if plan is None:
                raise StateError("批次缺少已完成的抽样计划")
            concession = None
            if plan["result"] == ItemResult.FAIL:
                if concession_id is None:
                    raise StateError("检验不合格批次必须凭已批准的让步审批放行")
                concession_id = require_int("concession_id", concession_id, minimum=1)
                concession = one(conn.execute(
                    "SELECT * FROM concessions WHERE id = ?", (concession_id,)))
                if concession is None or concession["lot_no"] != lot_no:
                    raise NotFoundError(f"让步审批不存在：{concession_id}")
                if concession["status"] != ConcessionStatus.APPROVED:
                    raise StateError(f"让步审批状态 {concession['status']} 不可用于放行")
                if take > concession["qty"] - concession["consumed_qty"]:
                    raise StateError("放行数量超出让步审批剩余数量")
                new_consumed = concession["consumed_qty"] + take
                new_cstatus = (ConcessionStatus.CONSUMED if new_consumed == concession["qty"]
                               else ConcessionStatus.APPROVED)
                cur = conn.execute(
                    "UPDATE concessions SET consumed_qty = ?, status = ?"
                    " WHERE id = ? AND status = 'APPROVED'",
                    (new_consumed, new_cstatus, concession["id"]))
                if cur.rowcount != 1:
                    raise StateError("让步审批状态已变化，请重试")
                dtype = DecisionType.CONCESSION_RELEASE
            else:
                if concession_id is not None:
                    raise ValidationError("检验合格批次不需要让步审批")
                dtype = DecisionType.RELEASE if take == remaining else DecisionType.PARTIAL_RELEASE
            # 1) 验收决定落库
            decision_id = conn.execute(
                "INSERT INTO decisions (lot_no, round, dtype, qty, reason, concession_id,"
                " request_id, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (lot_no, lot["inspect_round"], dtype, take, reason,
                 concession["id"] if concession else None, request_id, actor, now)).lastrowid
            # 2) 隔离库存转移/拆分（源库位占用同步减少）
            moved = take_units(conn, lot_no=lot_no, qty=take, to_status=UnitStatus.AVAILABLE,
                               decision_id=decision_id, now=now, to_location=target_location)
            # 3) 目标库位占用（容量不足则整体回滚以上写入）
            change_occupancy(conn, target_location, take)
            # 4) 批次状态迁移
            remaining_after = remaining - take
            if remaining_after == 0:
                returned = one(conn.execute(
                    "SELECT COALESCE(SUM(qty), 0) AS qty FROM stock_units"
                    " WHERE lot_no = ? AND status = 'RETURNED'",
                    (lot_no,)))["qty"]
                new_status = LotStatus.CLOSED if returned > 0 else LotStatus.RELEASED
            else:
                new_status = LotStatus.PARTIALLY_RELEASED
            cur = conn.execute(
                "UPDATE lots SET status = ?, updated_at = ? WHERE lot_no = ? AND status IN (?, ?)",
                (new_status, now, lot_no, *_DISPOSABLE))
            if cur.rowcount != 1:
                raise StateError("批次状态已变化，请重试")
            # 5) 审计事件
            add_event(conn, now=now, lot_no=lot_no, entity="decision", entity_id=decision_id,
                      action="DECISION_MADE", actor=actor,
                      detail={"dtype": dtype, "qty": take, "reason": reason,
                              "concession_id": concession["id"] if concession else None})
            for unit in moved:
                add_event(conn, now=now, lot_no=lot_no, entity="stock_unit",
                          entity_id=unit["unit_id"], action="UNIT_MOVED", actor=actor,
                          detail={"unit_code": unit["unit_code"], "qty": unit["qty"],
                                  "to_status": UnitStatus.AVAILABLE,
                                  "to_location": target_location, "decision_id": decision_id})
            decision = one(conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)))
            return {"replayed": False, "decision": decision,
                    "lot": get_lot(conn, lot_no), "units_moved": moved}

    # ------------------------------------------------------------------ 退货
    def return_to_supplier(self, *, lot_no: str, actor: str, reason: str,
                           qty: int | None = None, request_id: str | None = None) -> dict:
        """退货：剩余隔离库存（或指定数量）退回供应商，原子完成。"""
        lot_no = require_text("lot_no", lot_no)
        actor = require_text("actor", actor)
        reason = require_text("reason", reason)
        request_id = optional_text("request_id", request_id)
        now = self._now()
        with self._db.transaction() as conn:
            if request_id is not None:
                existing = one(conn.execute(
                    "SELECT * FROM decisions WHERE lot_no = ? AND request_id = ?",
                    (lot_no, request_id)))
                if existing is not None:
                    return {"replayed": True, "decision": existing,
                            "lot": get_lot(conn, lot_no), "units_moved": []}
            lot = get_lot(conn, lot_no)
            if lot["status"] not in _DISPOSABLE:
                raise StateError(f"批次状态 {lot['status']} 不允许退货")
            remaining = quarantined_qty(conn, lot_no)
            take = remaining if qty is None else require_int("qty", qty, minimum=1)
            if take > remaining:
                raise ValidationError(f"退货数量 {take} 超过待处置数量 {remaining}",
                                      details={"remaining": remaining})
            decision_id = conn.execute(
                "INSERT INTO decisions (lot_no, round, dtype, qty, reason, concession_id,"
                " request_id, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (lot_no, lot["inspect_round"], DecisionType.RETURN, take, reason,
                 request_id, actor, now)).lastrowid
            moved = take_units(conn, lot_no=lot_no, qty=take, to_status=UnitStatus.RETURNED,
                               decision_id=decision_id, now=now)
            remaining_after = remaining - take
            if remaining_after == 0:
                live = one(conn.execute(
                    "SELECT COALESCE(SUM(qty), 0) AS qty FROM stock_units"
                    " WHERE lot_no = ? AND status IN ('AVAILABLE', 'ISSUED')",
                    (lot_no,)))["qty"]
                new_status = LotStatus.CLOSED if live > 0 else LotStatus.RETURNED
            else:
                new_status = lot["status"]
            cur = conn.execute(
                "UPDATE lots SET status = ?, updated_at = ? WHERE lot_no = ? AND status IN (?, ?)",
                (new_status, now, lot_no, *_DISPOSABLE))
            if cur.rowcount != 1:
                raise StateError("批次状态已变化，请重试")
            add_event(conn, now=now, lot_no=lot_no, entity="decision", entity_id=decision_id,
                      action="DECISION_MADE", actor=actor,
                      detail={"dtype": DecisionType.RETURN, "qty": take, "reason": reason})
            for unit in moved:
                add_event(conn, now=now, lot_no=lot_no, entity="stock_unit",
                          entity_id=unit["unit_id"], action="UNIT_MOVED", actor=actor,
                          detail={"unit_code": unit["unit_code"], "qty": unit["qty"],
                                  "to_status": UnitStatus.RETURNED, "decision_id": decision_id})
            decision = one(conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)))
            return {"replayed": False, "decision": decision,
                    "lot": get_lot(conn, lot_no), "units_moved": moved}

    # ------------------------------------------------------------------ 复检
    def request_reinspect(self, *, lot_no: str, actor: str, reason: str) -> dict:
        """复检：记录复检决定，批次回到 INSPECTING，等待新一轮抽样计划。"""
        lot_no = require_text("lot_no", lot_no)
        actor = require_text("actor", actor)
        reason = require_text("reason", reason)
        now = self._now()
        with self._db.transaction() as conn:
            lot = get_lot(conn, lot_no)
            if lot["status"] not in _DISPOSABLE:
                raise StateError(f"批次状态 {lot['status']} 不允许复检")
            remaining = quarantined_qty(conn, lot_no)
            if remaining <= 0:
                raise StateError("没有可复检的隔离库存")
            decision_id = conn.execute(
                "INSERT INTO decisions (lot_no, round, dtype, qty, reason, concession_id,"
                " request_id, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (lot_no, lot["inspect_round"], DecisionType.REINSPECT, remaining,
                 reason, actor, now)).lastrowid
            cur = conn.execute(
                "UPDATE lots SET status = ?, updated_at = ? WHERE lot_no = ? AND status IN (?, ?)",
                (LotStatus.INSPECTING, now, lot_no, *_DISPOSABLE))
            if cur.rowcount != 1:
                raise StateError("批次状态已变化，请重试")
            add_event(conn, now=now, lot_no=lot_no, entity="decision", entity_id=decision_id,
                      action="DECISION_MADE", actor=actor,
                      detail={"dtype": DecisionType.REINSPECT, "qty": remaining,
                              "reason": reason})
            decision = one(conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)))
            return {"decision": decision, "lot": get_lot(conn, lot_no)}

    # -------------------------------------------------------------- 让步审批
    def request_concession(self, *, lot_no: str, qty: int, reason: str, actor: str) -> dict:
        """申请让步接收（待判定阶段的批次，数量不超过待处置数量）。"""
        lot_no = require_text("lot_no", lot_no)
        actor = require_text("actor", actor)
        reason = require_text("reason", reason)
        qty = require_int("qty", qty, minimum=1)
        now = self._now()
        with self._db.transaction() as conn:
            lot = get_lot(conn, lot_no)
            if lot["status"] not in _DISPOSABLE:
                raise StateError(f"批次状态 {lot['status']} 不允许申请让步")
            remaining = quarantined_qty(conn, lot_no)
            if qty > remaining:
                raise ValidationError(f"让步数量 {qty} 超过待处置数量 {remaining}",
                                      details={"remaining": remaining})
            concession_id = conn.execute(
                "INSERT INTO concessions (lot_no, qty, reason, status, requested_by, requested_at)"
                " VALUES (?, ?, ?, 'PENDING', ?, ?)",
                (lot_no, qty, reason, actor, now)).lastrowid
            add_event(conn, now=now, lot_no=lot_no, entity="concession",
                      entity_id=concession_id, action="CONCESSION_REQUESTED", actor=actor,
                      detail={"qty": qty, "reason": reason})
            return one(conn.execute("SELECT * FROM concessions WHERE id = ?", (concession_id,)))

    def approve_concession(self, *, concession_id: int, actor: str,
                           note: str | None = None) -> dict:
        """批准让步（审批人不得与申请人相同）。"""
        return self._decide_concession(concession_id=concession_id, actor=actor, note=note,
                                       approve=True)

    def reject_concession(self, *, concession_id: int, actor: str,
                          note: str | None = None) -> dict:
        """拒绝让步（审批人不得与申请人相同）。"""
        return self._decide_concession(concession_id=concession_id, actor=actor, note=note,
                                       approve=False)

    def _decide_concession(self, *, concession_id: int, actor: str, note: str | None,
                           approve: bool) -> dict:
        concession_id = require_int("concession_id", concession_id, minimum=1)
        actor = require_text("actor", actor)
        note = optional_text("note", note)
        now = self._now()
        with self._db.transaction() as conn:
            concession = one(conn.execute(
                "SELECT * FROM concessions WHERE id = ?", (concession_id,)))
            if concession is None:
                raise NotFoundError(f"让步审批不存在：{concession_id}")
            if concession["status"] != ConcessionStatus.PENDING:
                raise StateError(f"让步审批已处理（状态 {concession['status']}）")
            if actor == concession["requested_by"]:
                raise StateError("让步审批人不能与申请人相同")
            new_status = ConcessionStatus.APPROVED if approve else ConcessionStatus.REJECTED
            cur = conn.execute(
                "UPDATE concessions SET status = ?, decided_by = ?, decided_at = ?,"
                " decision_note = ? WHERE id = ? AND status = 'PENDING'",
                (new_status, actor, now, note, concession_id))
            if cur.rowcount != 1:
                raise StateError("让步审批状态已变化，请重试")
            add_event(conn, now=now, lot_no=concession["lot_no"], entity="concession",
                      entity_id=concession_id,
                      action="CONCESSION_APPROVED" if approve else "CONCESSION_REJECTED",
                      actor=actor, detail={"note": note})
            return one(conn.execute("SELECT * FROM concessions WHERE id = ?", (concession_id,)))
