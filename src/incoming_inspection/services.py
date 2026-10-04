"""领域服务：批次/抽检/检验/缺陷/让步/放行/领料。

所有写操作都在单个数据库事务内完成；触发器负责最终约束，服务层负责
前置校验、台账写入与批次聚合状态推进。任何一步失败，整笔事务回滚，
不会留下"半上架"状态。
"""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

from . import models as m
from .store import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex


def _raise_translated(exc: Exception) -> None:
    """把数据库触发器抛出的 IntegrityError 转译为业务冲突。"""
    if isinstance(exc, sqlite3.IntegrityError):
        message = str(exc)
        # UNIQUE/CHECK 等约束同样视为规则冲突；触发器 RAISE(ABORT) 带中文说明
        raise m.DomainError(message or "数据库约束冲突") from exc
    raise exc


class Service:
    def __init__(self, db: Database | str = "data/incoming.sqlite3") -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    # ---- 基础工具 ---------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        return self.db.connect()

    @staticmethod
    def _row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise m.NotFoundError("资源不存在")
        return row

    @staticmethod
    def _list(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(conn.execute(sql, params).fetchall())

    # ---- 库位 -------------------------------------------------------------
    def create_location(self, code: str, zone: str, capacity: int | None,
                        actor: str | None = None) -> dict:
        if zone not in ("隔离", "合格"):
            raise m.DomainError("库位分区必须是 隔离 或 合格")
        if capacity is not None and capacity <= 0:
            raise m.DomainError("容量必须大于 0")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            loc_id = _new_id()
            conn.execute(
                "INSERT INTO locations(id, code, zone, capacity, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (loc_id, code, zone, capacity, _now()),
            )
            self._event(conn, "location", loc_id, "location.created", actor,
                        {"code": code, "zone": zone})
            conn.commit()
            return self.get_location(loc_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def get_location(self, loc_id: str) -> dict:
        conn = self._conn()
        try:
            return dict(self._row(conn, "SELECT * FROM locations WHERE id=?", (loc_id,)))
        finally:
            conn.close()

    def list_locations(self, zone: str | None = None) -> list[dict]:
        conn = self._conn()
        try:
            if zone:
                rows = self._list(conn, "SELECT * FROM locations WHERE zone=? ORDER BY code", (zone,))
            else:
                rows = self._list(conn, "SELECT * FROM locations ORDER BY zone, code")
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- 批次与到货 -------------------------------------------------------
    def create_batch(self, batch_no: str, part_no: str, part_name: str,
                     supplier: str, qty: int, po_no: str | None = None,
                     actor: str | None = None) -> dict:
        if qty <= 0:
            raise m.DomainError("批次数量必须大于 0")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch_id = _new_id()
            conn.execute(
                "INSERT INTO batches(id, batch_no, po_no, part_no, part_name, supplier,"
                " qty, status, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, batch_no, po_no, part_no, part_name, supplier, qty,
                 m.BATCH_DRAFT, actor, _now(), _now()),
            )
            self._event(conn, "batch", batch_id, "batch.created", actor,
                        {"batch_no": batch_no, "qty": qty})
            conn.commit()
            return self.get_batch(batch_id)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise m.DomainError(f"批次号已存在：{batch_no}") from exc
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def get_batch(self, batch_id: str) -> dict:
        conn = self._conn()
        try:
            batch = dict(self._row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,)))
            batch["units"] = [dict(r) for r in self._list(
                conn,
                "SELECT su.*, l.code AS location_code FROM stock_units su "
                "LEFT JOIN locations l ON l.id = su.location_id "
                "WHERE su.batch_id=? ORDER BY su.unit_no", (batch_id,))]
            agg = conn.execute(
                "SELECT decision, COUNT(*) c FROM stock_units WHERE batch_id=? GROUP BY decision",
                (batch_id,)).fetchall()
            batch["decision_summary"] = {r["decision"]: r["c"] for r in agg}
            return batch
        finally:
            conn.close()

    def list_batches(self, status: str | None = None) -> list[dict]:
        conn = self._conn()
        try:
            if status:
                rows = self._list(conn, "SELECT * FROM batches WHERE status=? ORDER BY created_at",
                                  (status,))
            else:
                rows = self._list(conn, "SELECT * FROM batches ORDER BY created_at")
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def confirm_arrival(self, batch_id: str, quarantine_location_id: str,
                        received_qty: int | None = None,
                        actor: str | None = None) -> dict:
        """到货确认：在隔离库位上逐件生成库存单位，原子完成。"""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = self._row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
            if batch["status"] != m.BATCH_DRAFT:
                raise m.DomainError(f"仅草拟批次可确认到货，当前为 {batch['status']}")
            loc = self._row(conn, "SELECT * FROM locations WHERE id=?",
                            (quarantine_location_id,))
            if loc["zone"] != "隔离":
                raise m.DomainError("到货单位必须进入隔离区库位")
            qty = received_qty or batch["qty"]
            if received_qty is not None and received_qty != batch["qty"]:
                # 允许短溢装，但必须为正数
                if received_qty <= 0:
                    raise m.DomainError("到货数量必须大于 0")
            now = _now()
            for unit_no in range(1, qty + 1):
                conn.execute(
                    "INSERT INTO stock_units(id, batch_id, unit_no, status, decision,"
                    " location_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), batch_id, unit_no, m.STK_QUARANTINE, m.D_PENDING,
                     quarantine_location_id, now),
                )
            conn.execute(
                "UPDATE batches SET status=?, received_qty=?, received_at=?, updated_at=? "
                "WHERE id=?",
                (m.BATCH_RECEIVED, qty, now, now, batch_id),
            )
            self._event(conn, "batch", batch_id, "batch.arrived", actor,
                        {"received_qty": qty, "location": loc["code"]})
            conn.commit()
            return self.get_batch(batch_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    # ---- 抽样计划 ---------------------------------------------------------
    def create_sampling_plan(self, batch_id: str, sample_size: int,
                             plan_type: str = "正常抽检", aql: str | None = None,
                             actor: str | None = None) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = self._row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
            if sample_size <= 0:
                raise m.DomainError("抽样数量必须大于 0")
            if batch["status"] == m.BATCH_DRAFT:
                raise m.DomainError("批次尚未到货，不能创建抽样计划")
            if batch["status"] in (m.BATCH_ACCEPTED, m.BATCH_REJECTED, m.BATCH_CLOSED):
                raise m.DomainError(f"批次已终态（{batch['status']}），不能创建抽样计划")
            rounds = conn.execute(
                "SELECT COALESCE(MAX(round), 0) AS m FROM sampling_plans WHERE batch_id=?",
                (batch_id,)).fetchone()["m"]
            next_round = rounds + 1
            if next_round == 1 and batch["status"] != m.BATCH_RECEIVED:
                # 触发器已兜底，这里给出更准确的应用层信息
                raise m.DomainError("首检计划只能在待确认批次上创建")
            if next_round > 1:
                # 复检轮次：批次必须先进入复检中
                if batch["status"] != m.BATCH_REINSPECTING:
                    raise m.DomainError("发起复检后才能创建复检抽样计划")
            plan_id = _new_id()
            conn.execute(
                "INSERT INTO sampling_plans(id, batch_id, round, plan_type, sample_size,"
                " aql, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, batch_id, next_round, plan_type, sample_size, aql,
                 m.PLAN_PENDING, actor, _now()),
            )
            self._event(conn, "sampling_plan", plan_id, "plan.created", actor,
                        {"batch_no": batch["batch_no"], "round": next_round})
            conn.commit()
            return self.get_sampling_plan(plan_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def _get_plan(self, conn: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
        return self._row(conn, "SELECT * FROM sampling_plans WHERE id=?", (plan_id,))

    def get_sampling_plan(self, plan_id: str) -> dict:
        conn = self._conn()
        try:
            plan = dict(self._get_plan(conn, plan_id))
            plan["items"] = [dict(r) for r in self._list(
                conn,
                "SELECT i.*, (SELECT COUNT(*) FROM defect_evidences e WHERE e.item_id=i.id)"
                " AS evidence_count FROM inspection_items i WHERE i.plan_id=? ORDER BY i.seq",
                (plan_id,))]
            return plan
        finally:
            conn.close()

    def list_sampling_plans(self, batch_id: str) -> list[dict]:
        conn = self._conn()
        try:
            return [dict(r) for r in self._list(
                conn, "SELECT * FROM sampling_plans WHERE batch_id=? ORDER BY round",
                (batch_id,))]
        finally:
            conn.close()

    def start_sampling(self, plan_id: str, actor: str | None = None) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = self._get_plan(conn, plan_id)
            if plan["status"] != m.PLAN_PENDING:
                raise m.DomainError(f"仅待检计划可开始，当前为 {plan['status']}")
            conn.execute(
                "UPDATE sampling_plans SET status=?, started_at=COALESCE(started_at, ?) "
                "WHERE id=?",
                (m.PLAN_INSPECTING, _now(), plan_id),
            )
            self._event(conn, "sampling_plan", plan_id, "plan.started", actor, None)
            conn.commit()
            return self.get_sampling_plan(plan_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def add_inspection_item(self, plan_id: str, name: str,
                            spec_requirement: str | None = None) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = self._get_plan(conn, plan_id)
            if plan["status"] == m.PLAN_JUDGED:
                raise m.DomainError("计划已判定，不能追加检验项")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM inspection_items WHERE plan_id=?",
                (plan_id,)).fetchone()["s"]
            item_id = _new_id()
            conn.execute(
                "INSERT INTO inspection_items(id, plan_id, seq, name, spec_requirement, status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, plan_id, seq, name, spec_requirement, m.INSPECTION_PENDING),
            )
            conn.commit()
            return self.get_inspection_item(item_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def get_inspection_item(self, item_id: str) -> dict:
        conn = self._conn()
        try:
            item = dict(self._row(conn, "SELECT * FROM inspection_items WHERE id=?",
                                  (item_id,)))
            item["evidences"] = [dict(r) for r in self._list(
                conn, "SELECT * FROM defect_evidences WHERE item_id=? ORDER BY created_at",
                (item_id,))]
            return item
        finally:
            conn.close()

    def record_inspection(self, item_id: str, result: str,
                          measured_value: str | None = None,
                          evidences: Iterable[dict] = (),
                          actor: str | None = None) -> dict:
        """录入检验项结果与缺陷证据（同事务）。

        evidences: [{"is_defect": bool, "severity": "致命|严重|轻微|None",
                     "evidence_ref": str, "description": str}]
        任一证据不合法则整笔回滚，不会出现"结果已录、证据缺失"。
        """
        if result not in ("合格", "缺陷"):
            raise m.DomainError("检验项结果必须为 合格 或 缺陷")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = self._row(conn, "SELECT * FROM inspection_items WHERE id=?", (item_id,))
            plan = self._get_plan(conn, item["plan_id"])
            if plan["status"] == m.PLAN_JUDGED:
                raise m.DomainError("计划已判定，不能再录入检验结果")
            if item["status"] == m.INSPECTION_DONE:
                raise m.DomainError("检验项已完成，结果不可更改")
            evidences = list(evidences)
            if result == "缺陷" and not any(e.get("is_defect") for e in evidences):
                raise m.DomainError("缺陷结果必须附至少一条缺陷证据")
            now = _now()
            # 自动把计划推进到检验中
            if plan["status"] == m.PLAN_PENDING:
                conn.execute(
                    "UPDATE sampling_plans SET status=?, started_at=COALESCE(started_at, ?)"
                    " WHERE id=?",
                    (m.PLAN_INSPECTING, now, item["plan_id"]),
                )
            conn.execute(
                "UPDATE inspection_items SET status=?, result=?, measured_value=?,"
                " checked_by=?, checked_at=? WHERE id=?",
                (m.INSPECTION_DONE, result, measured_value, actor, now, item_id),
            )
            for e in evidences:
                is_defect = 1 if e.get("is_defect") else 0
                sev = e.get("severity")
                if is_defect and sev not in m.SEVERITIES:
                    raise m.DomainError("缺陷证据必须标注严重度：致命/严重/轻微")
                if not is_defect and sev is not None:
                    raise m.DomainError("非缺陷证据不得标注严重度")
                conn.execute(
                    "INSERT INTO defect_evidences(id, item_id, is_defect, severity,"
                    " evidence_ref, description, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), item_id, is_defect, sev, e.get("evidence_ref"),
                     e.get("description"), actor, now),
                )
            self._event(conn, "inspection_item", item_id, "item.recorded", actor,
                        {"result": result})
            conn.commit()
            return self.get_inspection_item(item_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def judge_sampling(self, plan_id: str, result: str,
                       actor: str | None = None) -> dict:
        """质量工程师判定抽检计划。"""
        if result not in m.JUDGE_CHOICES:
            raise m.DomainError("判定结果必须为 合格 或 不合格")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = self._get_plan(conn, plan_id)
            if plan["status"] == m.PLAN_JUDGED:
                raise m.DomainError("计划已判定，不可重复判定")
            items = self._list(conn,
                               "SELECT * FROM inspection_items WHERE plan_id=?",
                               (plan_id,))
            if not items:
                raise m.DomainError("没有检验项的计划不能判定")
            pending = [i for i in items if i["status"] != m.INSPECTION_DONE]
            if pending:
                raise m.DomainError(f"仍有 {len(pending)} 个检验项未完成")
            # 应用层一致性：判定结论与检验项证据矛盾时拒绝
            has_defect = any(i["result"] == "缺陷" for i in items)
            if result == m.JUDGE_ACCEPT and has_defect:
                raise m.DomainError("检验项中存在缺陷，不能判定为合格")
            if result == m.JUDGE_REJECT and not has_defect:
                raise m.DomainError("检验项全部合格，不能判定为不合格")
            now = _now()
            conn.execute(
                "UPDATE sampling_plans SET status=?, result=?, judged_by=?, judged_at=?"
                " WHERE id=?",
                (m.PLAN_JUDGED, result, actor, now, plan_id),
            )
            self._event(conn, "sampling_plan", plan_id, "plan.judged", actor,
                        {"result": result})
            conn.commit()
            return self.get_sampling_plan(plan_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def request_reinspection(self, batch_id: str, reason: str,
                             actor: str | None = None) -> dict:
        """首检不合格后（或部分放行后）将批次转入复检中。"""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = self._row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
            failed = self._list(conn,
                                "SELECT * FROM sampling_plans WHERE batch_id=? AND result='不合格'"
                                " ORDER BY round", (batch_id,))
            if not failed:
                raise m.DomainError("不存在不合格抽检计划，不能发起复检")
            if batch["status"] not in (m.BATCH_RECEIVED, m.BATCH_PARTIAL):
                raise m.DomainError(f"当前状态 {batch['status']} 不允许发起复检")
            conn.execute(
                "UPDATE batches SET status=?, updated_at=? WHERE id=?",
                (m.BATCH_REINSPECTING, _now(), batch_id),
            )
            self._event(conn, "batch", batch_id, "batch.reinspect_requested", actor,
                        {"reason": reason, "after_round": failed[-1]["round"]})
            conn.commit()
            return self.get_batch(batch_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    # ---- 让步审批 ---------------------------------------------------------
    def request_concession(self, stock_unit_id: str, reason: str,
                           proposed_use: str | None = None,
                           actor: str | None = None) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            unit = self._row(conn, "SELECT * FROM stock_units WHERE id=?",
                             (stock_unit_id,))
            batch = self._row(conn, "SELECT * FROM batches WHERE id=?",
                              (unit["batch_id"],))
            if unit["decision"] != m.D_PENDING:
                raise m.DomainError("只能对未判定单位申请让步")
            if batch["status"] in (m.BATCH_REJECTED, m.BATCH_CLOSED):
                raise m.DomainError(f"批次状态 {batch['status']}，不能申请让步")
            judged = conn.execute(
                "SELECT COUNT(*) c FROM sampling_plans WHERE batch_id=? AND status='已判定'",
                (batch["id"],)).fetchone()["c"]
            if judged == 0:
                raise m.DomainError("让步申请必须在抽检判定之后提出")
            cid = _new_id()
            conn.execute(
                "INSERT INTO concessions(id, stock_unit_id, reason, proposed_use, status,"
                " requested_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, stock_unit_id, reason, proposed_use, m.CONCESSION_PENDING,
                 actor, _now()),
            )
            self._event(conn, "concession", cid, "concession.requested", actor,
                        {"unit": stock_unit_id})
            conn.commit()
            return self.get_concession(cid)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise m.DomainError("该单位已存在审批中的让步申请") from exc
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def decide_concession(self, concession_id: str, approve: bool,
                          decision_note: str | None = None,
                          actor: str | None = None) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            con = self._row(conn, "SELECT * FROM concessions WHERE id=?",
                            (concession_id,))
            if con["status"] != m.CONCESSION_PENDING:
                raise m.DomainError(f"让步申请已终结（{con['status']}），不可更改")
            new_status = m.CONCESSION_APPROVED if approve else m.CONCESSION_REJECTED
            conn.execute(
                "UPDATE concessions SET status=?, decided_by=?, decided_at=?, decision_note=?"
                " WHERE id=?",
                (new_status, actor, _now(), decision_note, concession_id),
            )
            self._event(conn, "concession", concession_id,
                        "concession.approved" if approve else "concession.rejected",
                        actor, None)
            conn.commit()
            return self.get_concession(concession_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def get_concession(self, concession_id: str) -> dict:
        conn = self._conn()
        try:
            return dict(self._row(conn, "SELECT * FROM concessions WHERE id=?",
                                  (concession_id,)))
        finally:
            conn.close()

    # ---- 放行（原子：决定 + 上架 + 批次聚合）-------------------------------
    def release_units(self, batch_id: str, unit_ids: list[str],
                      available_location_id: str,
                      decision: str = m.D_ACCEPT,
                      concession_ids: dict[str, str] | None = None,
                      actor: str | None = None) -> dict:
        """把隔离单位放行到合格库位。

        - 合格放行：批次最新一轮抽检必须判定为合格。
        - 让步接收：每个单位须附已批准的让步审批单。
        - 决定写入、库位移动、批次状态聚合在同一事务；任一失败整体回滚。
        """
        if decision not in (m.D_ACCEPT, m.D_CONCESSION):
            raise m.DomainError("放行决定必须为 合格放行 或 让步接收")
        if not unit_ids:
            raise m.DomainError("放行单位列表不能为空")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = self._row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
            if batch["status"] in (m.BATCH_ACCEPTED, m.BATCH_REJECTED, m.BATCH_CLOSED):
                raise m.DomainError(f"批次已终态（{batch['status']}），不能放行")
            loc = self._row(conn, "SELECT * FROM locations WHERE id=?",
                            (available_location_id,))
            if loc["zone"] != "合格":
                raise m.DomainError("放行目标必须是合格区库位")
            latest_plan = conn.execute(
                "SELECT * FROM sampling_plans WHERE batch_id=? AND status='已判定'"
                " ORDER BY round DESC LIMIT 1", (batch_id,)).fetchone()
            now = _now()
            released = 0
            for uid in dict.fromkeys(unit_ids):  # 去重保序
                unit = self._row(conn,
                                 "SELECT * FROM stock_units WHERE id=?", (uid,))
                if unit["batch_id"] != batch_id:
                    raise m.DomainError(f"单位 {uid} 不属于该批次")
                if unit["decision"] != m.D_PENDING:
                    raise m.DomainError(f"单位 {unit['unit_no']} 已判定为"
                                        f" {unit['decision']}，不能重复放行")
                if unit["status"] != m.STK_QUARANTINE:
                    raise m.DomainError(f"单位 {unit['unit_no']} 不在隔离状态")

                basis_type = "抽样计划"
                basis_id = latest_plan["id"] if latest_plan else ""
                if decision == m.D_CONCESSION:
                    cid = (concession_ids or {}).get(uid)
                    if not cid:
                        raise m.DomainError(
                            f"单位 {unit['unit_no']} 让步接收缺少让步审批单")
                    con = self._row(conn, "SELECT * FROM concessions WHERE id=?", (cid,))
                    if con["stock_unit_id"] != uid:
                        raise m.DomainError("让步审批单与单位不匹配")
                    if con["status"] != m.CONCESSION_APPROVED:
                        raise m.DomainError(
                            f"单位 {unit['unit_no']} 的让步审批未批准（{con['status']}）")
                    basis_type = "让步审批"
                    basis_id = cid
                elif latest_plan is None:
                    raise m.DomainError("批次尚无已判定的抽样计划，不能合格放行")
                elif latest_plan["result"] != m.JUDGE_ACCEPT:
                    raise m.DomainError(
                        f"最新一轮（第 {latest_plan['round']} 轮）抽检不合格，"
                        "不能合格放行；可退货或让步接收")

                # 1) 写决定（触发器校验：让步凭证/计划依据/一次性）
                conn.execute(
                    "UPDATE stock_units SET decision=?, updated_at=? WHERE id=?",
                    (decision, now, uid),
                )
                conn.execute(
                    "INSERT INTO decisions(id, stock_unit_id, from_decision, to_decision,"
                    " basis_type, basis_id, reason, actor, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), uid, m.D_PENDING, decision, basis_type, basis_id,
                     None, actor, now),
                )
                # 2) 物理上架到合格库位（触发器校验：隔离边界/容量/分区）
                conn.execute(
                    "UPDATE stock_units SET status=?, location_id=?, updated_at=? WHERE id=?",
                    (m.STK_AVAILABLE, available_location_id, now, uid),
                )
                conn.execute(
                    "INSERT INTO inventory_movements(id, stock_unit_id, from_status,"
                    " to_status, location_id, basis_type, basis_id, actor, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), uid, m.STK_QUARANTINE, m.STK_AVAILABLE,
                     available_location_id, basis_type, basis_id, actor, now),
                )
                released += 1

            self._recompute_batch_status(conn, batch_id, actor, now)
            self._event(conn, "batch", batch_id, "batch.released", actor,
                        {"count": released, "decision": decision,
                         "location": loc["code"]})
            conn.commit()
            return self.get_batch(batch_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def reject_units(self, batch_id: str, unit_ids: list[str],
                     reason: str | None = None, actor: str | None = None) -> dict:
        """把单位判退并退货出库（原子：决定 + 出库 + 批次聚合）。"""
        if not unit_ids:
            raise m.DomainError("退货单位列表不能为空")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = self._row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
            if batch["status"] in (m.BATCH_ACCEPTED, m.BATCH_CLOSED):
                raise m.DomainError(f"批次状态 {batch['status']}，不能退货")
            latest_plan = conn.execute(
                "SELECT * FROM sampling_plans WHERE batch_id=? AND status='已判定'"
                " ORDER BY round DESC LIMIT 1", (batch_id,)).fetchone()
            now = _now()
            rejected = 0
            for uid in dict.fromkeys(unit_ids):
                unit = self._row(conn, "SELECT * FROM stock_units WHERE id=?", (uid,))
                if unit["batch_id"] != batch_id:
                    raise m.DomainError(f"单位 {uid} 不属于该批次")
                if unit["decision"] != m.D_PENDING:
                    raise m.DomainError(f"单位 {unit['unit_no']} 已判定，不能退货")
                if unit["status"] != m.STK_QUARANTINE:
                    raise m.DomainError(f"单位 {unit['unit_no']} 不在隔离状态")
                if latest_plan is None:
                    raise m.DomainError("批次尚无已判定的抽样计划，不能退货")
                if latest_plan["result"] != m.JUDGE_REJECT:
                    raise m.DomainError(
                        f"最新一轮（第 {latest_plan['round']} 轮）抽检合格，不能退货")
                # 1) 决定
                conn.execute(
                    "UPDATE stock_units SET decision=?, updated_at=? WHERE id=?",
                    (m.D_REJECT, now, uid),
                )
                conn.execute(
                    "INSERT INTO decisions(id, stock_unit_id, from_decision, to_decision,"
                    " basis_type, basis_id, reason, actor, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), uid, m.D_PENDING, m.D_REJECT, "抽样计划",
                     latest_plan["id"], reason, actor, now),
                )
                # 2) 出库（触发器校验状态迁移）
                conn.execute(
                    "UPDATE stock_units SET status=?, updated_at=? WHERE id=?",
                    (m.STK_RETURNED, now, uid),
                )
                conn.execute(
                    "INSERT INTO inventory_movements(id, stock_unit_id, from_status,"
                    " to_status, location_id, basis_type, basis_id, actor, note, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), uid, m.STK_QUARANTINE, m.STK_RETURNED,
                     unit["location_id"], "抽样计划", latest_plan["id"],
                     actor, reason, now),
                )
                rejected += 1
            self._recompute_batch_status(conn, batch_id, actor, now)
            self._event(conn, "batch", batch_id, "batch.rejected", actor,
                        {"count": rejected})
            conn.commit()
            return self.get_batch(batch_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def _recompute_batch_status(self, conn: sqlite3.Connection, batch_id: str,
                                actor: str | None, now: str) -> None:
        """根据单位决定聚合推进批次状态。

        触发器 trg_batch_status_consistency 会兜底校验聚合一致性。
        待确认批次在第一次出现决定时 → 部分放行/全部放行/全部退货；
        复检中/部分放行同理；全部退货时同步关闭到已关闭（单位均已出库）。
        """
        counts = {r["decision"]: r["c"] for r in conn.execute(
            "SELECT decision, COUNT(*) c FROM stock_units WHERE batch_id=? GROUP BY decision",
            (batch_id,))}
        total = sum(counts.values())
        pending = counts.get(m.D_PENDING, 0)
        released = counts.get(m.D_ACCEPT, 0) + counts.get(m.D_CONCESSION, 0)
        rejected = counts.get(m.D_REJECT, 0)
        old = conn.execute("SELECT status FROM batches WHERE id=?",
                           (batch_id,)).fetchone()["status"]
        if released and pending == 0 and rejected == 0:
            new_status = m.BATCH_ACCEPTED
        elif rejected == total and total > 0:
            new_status = m.BATCH_REJECTED
        elif released or rejected:
            new_status = m.BATCH_PARTIAL
        else:
            return  # 无任何决定，保持原状态
        if new_status != old:
            conn.execute("UPDATE batches SET status=?, updated_at=? WHERE id=?",
                         (new_status, now, batch_id))
            self._event(conn, "batch", batch_id, "batch.status_changed", actor,
                        {"from": old, "to": new_status})
        # 全部退货且所有单位已出库 → 关闭
        if new_status == m.BATCH_REJECTED:
            outstanding = conn.execute(
                "SELECT COUNT(*) c FROM stock_units WHERE batch_id=? AND status != '已退货'",
                (batch_id,)).fetchone()["c"]
            if outstanding == 0:
                conn.execute("UPDATE batches SET status=?, updated_at=? WHERE id=?",
                             (m.BATCH_CLOSED, now, batch_id))
                self._event(conn, "batch", batch_id, "batch.closed", actor,
                            {"reason": "全部退货出库完成"})

    # ---- 领料预留（并发安全）----------------------------------------------
    def create_reservation(self, stock_unit_id: str, work_order: str,
                           actor: str | None = None) -> dict:
        """并发领料：通过事务 + 条件 UPDATE 抢占，同一单位只能被一张工单预留。"""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            unit = self._row(conn, "SELECT * FROM stock_units WHERE id=?",
                             (stock_unit_id,))
            if unit["decision"] not in (m.D_ACCEPT, m.D_CONCESSION):
                raise m.DomainError("仅已放行单位可领料")
            if unit["status"] != m.STK_AVAILABLE:
                raise m.DomainError(f"单位当前状态 {unit['status']}，不可预留"
                                    "（可能已被其他工单并发抢占）")
            rid = _new_id()
            res_no = "RSV-" + uuid.uuid4().hex[:12].upper()
            now = _now()
            # 先建预留单（触发器此时检查单位为可用）
            conn.execute(
                "INSERT INTO stock_reservations(id, reservation_no, stock_unit_id,"
                " work_order, status, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, res_no, stock_unit_id, work_order, "已预留", actor, now),
            )
            # 再条件更新单位；并发下只有一个事务能走到这里
            cur = conn.execute(
                "UPDATE stock_units SET status=?, updated_at=? WHERE id=? AND status=?",
                (m.STK_RESERVED, now, stock_unit_id, m.STK_AVAILABLE),
            )
            if cur.rowcount != 1:
                raise m.DomainError("单位已被并发占用，预留失败")
            conn.execute(
                "INSERT INTO inventory_movements(id, stock_unit_id, from_status,"
                " to_status, basis_type, basis_id, actor, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_new_id(), stock_unit_id, m.STK_AVAILABLE, m.STK_RESERVED,
                 "领料预留", rid, actor, work_order, now),
            )
            self._event(conn, "reservation", rid, "reservation.created", actor,
                        {"work_order": work_order})
            conn.commit()
            return self.get_reservation(rid)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def issue_reservation(self, reservation_id: str,
                          actor: str | None = None) -> dict:
        """发放预留：单位 已预留 → 已领料，原子。"""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            res = self._row(conn, "SELECT * FROM stock_reservations WHERE id=?",
                            (reservation_id,))
            if res["status"] != "已预留":
                raise m.DomainError(f"预留单状态 {res['status']}，不能发放")
            unit = self._row(conn, "SELECT * FROM stock_units WHERE id=?",
                             (res["stock_unit_id"],))
            if unit["status"] != m.STK_RESERVED:
                raise m.DomainError(f"单位当前状态 {unit['status']}，不能发放领料")
            now = _now()
            # 先把预留单置为已领料，单位触发器要求凭证状态匹配
            conn.execute(
                "UPDATE stock_reservations SET status=?, issued_at=? WHERE id=?",
                ("已领料", now, reservation_id),
            )
            conn.execute(
                "UPDATE stock_units SET status=?, updated_at=? WHERE id=?",
                (m.STK_ISSUED, now, res["stock_unit_id"]),
            )
            conn.execute(
                "INSERT INTO inventory_movements(id, stock_unit_id, from_status,"
                " to_status, basis_type, basis_id, actor, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_new_id(), res["stock_unit_id"], m.STK_RESERVED, m.STK_ISSUED,
                 "领料预留", reservation_id, actor, res["work_order"], now),
            )
            self._event(conn, "reservation", reservation_id, "reservation.issued",
                        actor, {"work_order": res["work_order"]})
            conn.commit()
            return self.get_reservation(reservation_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def cancel_reservation(self, reservation_id: str,
                           actor: str | None = None) -> dict:
        """取消预留：单位回到可用，供其他工单领用。"""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            res = self._row(conn, "SELECT * FROM stock_reservations WHERE id=?",
                            (reservation_id,))
            if res["status"] != "已预留":
                raise m.DomainError(f"预留单状态 {res['status']}，不能取消")
            now = _now()
            # 先取消凭证（单位回可用时触发器要求不存在有效预留单）
            conn.execute(
                "UPDATE stock_reservations SET status=? WHERE id=?",
                ("已取消", reservation_id),
            )
            conn.execute(
                "UPDATE stock_units SET status=?, updated_at=? WHERE id=? AND status=?",
                (m.STK_AVAILABLE, now, res["stock_unit_id"], m.STK_RESERVED),
            )
            conn.execute(
                "INSERT INTO inventory_movements(id, stock_unit_id, from_status,"
                " to_status, basis_type, basis_id, actor, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_new_id(), res["stock_unit_id"], m.STK_RESERVED, m.STK_AVAILABLE,
                 "领料预留", reservation_id, actor, "取消预留", now),
            )
            self._event(conn, "reservation", reservation_id, "reservation.cancelled",
                        actor, None)
            conn.commit()
            return self.get_reservation(reservation_id)
        except Exception as exc:
            conn.rollback()
            _raise_translated(exc)
        finally:
            conn.close()

    def get_reservation(self, reservation_id: str) -> dict:
        conn = self._conn()
        try:
            return dict(self._row(conn, "SELECT * FROM stock_reservations WHERE id=?",
                                  (reservation_id,)))
        finally:
            conn.close()

    # ---- 追溯 -------------------------------------------------------------
    def trace_unit(self, stock_unit_id: str) -> dict:
        """沿批次追踪单个库存单位的完整决定依据链。"""
        conn = self._conn()
        try:
            unit = dict(self._row(
                conn,
                "SELECT su.*, b.batch_no, b.part_no, b.part_name, b.supplier,"
                " l.code AS location_code FROM stock_units su"
                " JOIN batches b ON b.id = su.batch_id"
                " LEFT JOIN locations l ON l.id = su.location_id WHERE su.id=?",
                (stock_unit_id,)))
            decisions = []
            for d in self._list(conn,
                                "SELECT * FROM decisions WHERE stock_unit_id=?"
                                " ORDER BY rowid", (stock_unit_id,)):
                item = dict(d)
                item["basis"] = self._expand_basis(conn, d["basis_type"], d["basis_id"])
                decisions.append(item)
            unit["decisions"] = decisions
            unit["movements"] = [dict(r) for r in self._list(
                conn,
                "SELECT mv.*, l.code AS location_code FROM inventory_movements mv"
                " LEFT JOIN locations l ON l.id = mv.location_id"
                " WHERE mv.stock_unit_id=? ORDER BY mv.rowid",
                (stock_unit_id,))]
            cons = self._list(conn,
                              "SELECT * FROM concessions WHERE stock_unit_id=?"
                              " ORDER BY created_at", (stock_unit_id,))
            unit["concessions"] = [dict(r) for r in cons]
            res = self._list(conn,
                             "SELECT * FROM stock_reservations WHERE stock_unit_id=?"
                             " ORDER BY created_at", (stock_unit_id,))
            unit["reservations"] = [dict(r) for r in res]
            return unit
        finally:
            conn.close()

    def trace_batch(self, batch_id: str) -> dict:
        """批次级追溯：每单位的决定依据 + 批次所有检验轮次。"""
        conn = self._conn()
        try:
            batch = self.get_batch(batch_id)
            units = []
            for u in batch["units"]:
                units.append(self.trace_unit(u["id"]))
            plans = []
            for p in self._list(conn,
                                "SELECT * FROM sampling_plans WHERE batch_id=?"
                                " ORDER BY round", (batch_id,)):
                plan = dict(p)
                plan["items"] = [dict(r) for r in self._list(
                    conn,
                    "SELECT i.*, (SELECT json_group_array(json_object("
                    " 'id', e.id, 'is_defect', e.is_defect, 'severity', e.severity,"
                    " 'description', e.description)) FROM defect_evidences e"
                    " WHERE e.item_id=i.id) AS evidences"
                    " FROM inspection_items i WHERE i.plan_id=? ORDER BY i.seq",
                    (p["id"],))]
                plans.append(plan)
            batch["trace_units"] = units
            batch["plans"] = plans
            batch["events"] = [dict(r) for r in self._list(
                conn,
                "SELECT * FROM event_log WHERE entity_type='batch' AND entity_id=?"
                " ORDER BY rowid", (batch_id,))]
            return batch
        finally:
            conn.close()

    @staticmethod
    def _expand_basis(conn: sqlite3.Connection, basis_type: str,
                      basis_id: str) -> dict | None:
        if not basis_id:
            return None
        if basis_type in ("抽样计划", "质量判定"):
            row = conn.execute(
                "SELECT p.id, p.round, p.status, p.result, p.judged_by, p.judged_at,"
                " b.batch_no FROM sampling_plans p JOIN batches b ON b.id=p.batch_id"
                " WHERE p.id=?", (basis_id,)).fetchone()
            return dict(row) if row else None
        if basis_type == "让步审批":
            row = conn.execute("SELECT * FROM concessions WHERE id=?",
                               (basis_id,)).fetchone()
            return dict(row) if row else None
        if basis_type == "领料预留":
            row = conn.execute("SELECT * FROM stock_reservations WHERE id=?",
                               (basis_id,)).fetchone()
            return dict(row) if row else None
        return None

    def available_stock(self, part_no: str | None = None,
                        include_allocated: bool = False) -> list[dict]:
        """可用库存视图。

        默认仅返回状态为"可用"（可被生产领料）的已放行单位；已预留/已领料
        单位通过 include_allocated=True 一并返回。待判/隔离单位永不出现。
        """
        conn = self._conn()
        try:
            statuses = ("可用", "已预留", "已领料") if include_allocated else ("可用",)
            placeholders = ",".join("?" for _ in statuses)
            sql = ("SELECT su.id, su.status, su.decision, b.batch_no, b.part_no,"
                   " b.part_name, b.supplier, l.code AS location_code"
                   " FROM stock_units su JOIN batches b ON b.id=su.batch_id"
                   " LEFT JOIN locations l ON l.id=su.location_id"
                   f" WHERE su.status IN ({placeholders})"
                   " AND su.decision IN ('合格放行', '让步接收')")
            params: tuple = statuses
            if part_no:
                sql += " AND b.part_no=?"
                params = (*statuses, part_no)
            sql += " ORDER BY b.batch_no, su.unit_no"
            return [dict(r) for r in self._list(conn, sql, params)]
        finally:
            conn.close()

    # ---- 事件 -------------------------------------------------------------
    def _event(self, conn: sqlite3.Connection, entity_type: str, entity_id: str,
               event: str, actor: str | None, detail: object) -> None:
        import json
        conn.execute(
            "INSERT INTO event_log(id, entity_type, entity_id, event, actor, detail,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_new_id(), entity_type, entity_id, event, actor,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None,
             _now()),
        )
