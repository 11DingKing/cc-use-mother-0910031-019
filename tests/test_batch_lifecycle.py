"""批次检验状态机与到货约束。"""
from __future__ import annotations

import unittest

from scenario import ACTOR_KEEPER, ACTOR_QE, judge_plan, make_received_batch, make_service, unit_ids
from incoming_inspection.models import (
    BATCH_ACCEPTED, BATCH_DRAFT, BATCH_PARTIAL, BATCH_RECEIVED, BATCH_REJECTED,
    BATCH_REINSPECTING, BATCH_CLOSED, D_ACCEPT, D_PENDING, D_REJECT,
    STK_AVAILABLE, STK_QUARANTINE, DomainError,
)


class BatchStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_draft_to_received_creates_quarantine_units(self) -> None:
        b, q, _ = make_received_batch(self.svc, qty=4)
        self.assertEqual(b["status"], BATCH_RECEIVED)
        self.assertEqual(len(b["units"]), 4)
        self.assertTrue(all(u["status"] == STK_QUARANTINE for u in b["units"]))
        self.assertTrue(all(u["decision"] == D_PENDING for u in b["units"]))
        self.assertTrue(all(u["location_code"] == q["code"] for u in b["units"]))

    def test_arrival_twice_rejected(self) -> None:
        b, q, _ = make_received_batch(self.svc)
        with self.assertRaises(DomainError):
            self.svc.confirm_arrival(b["id"], q["id"], actor=ACTOR_KEEPER)

    def test_cannot_create_plan_before_arrival(self) -> None:
        svc = self.svc
        q = svc.create_location("Q1", "隔离", None)
        b = svc.create_batch("BX1", "P", "名", "S", 2)
        with self.assertRaises(DomainError):
            svc.create_sampling_plan(b["id"], 1, actor=ACTOR_QE)

    def test_quarantine_location_zone_enforced(self) -> None:
        svc = self.svc
        a = svc.create_location("A1", "合格", None)
        b = svc.create_batch("BX2", "P", "名", "S", 2)
        # 到货不能直接进合格区
        with self.assertRaises(DomainError):
            svc.confirm_arrival(b["id"], a["id"], actor=ACTOR_KEEPER)
        self.assertEqual(svc.get_batch(b["id"])["status"], BATCH_DRAFT)

    def test_quarantine_capacity_blocks_arrival_atomically(self) -> None:
        svc = self.svc
        q = svc.create_location("QCAP", "隔离", 2)
        b = svc.create_batch("BX3", "P", "名", "S", 5)
        with self.assertRaises(DomainError):
            svc.confirm_arrival(b["id"], q["id"], actor=ACTOR_KEEPER)
        fresh = svc.get_batch(b["id"])
        self.assertEqual(fresh["status"], BATCH_DRAFT)
        self.assertEqual(fresh["units"], [])  # 失败不留任何单位

    def test_full_accept_flow_reaches_terminal_state(self) -> None:
        b, _, a = make_received_batch(self.svc, qty=3)
        judge_plan(self.svc, b["id"], "合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        b = self.svc.release_units(b["id"], ids, a["id"], actor=ACTOR_KEEPER)
        self.assertEqual(b["status"], BATCH_ACCEPTED)
        self.assertEqual(set(b["decision_summary"]), {D_ACCEPT})
        # 终态不可再放行
        with self.assertRaises(DomainError):
            self.svc.release_units(b["id"], ids, a["id"], actor=ACTOR_KEEPER)

    def test_full_reject_flow_closes_batch(self) -> None:
        b, _, _ = make_received_batch(self.svc, qty=2)
        judge_plan(self.svc, b["id"], "不合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        b = self.svc.reject_units(b["id"], ids, reason="不合格", actor=ACTOR_QE)
        # 全部退货且出库完成 -> 已关闭
        self.assertEqual(b["status"], BATCH_CLOSED)

    def test_reinspection_transitions(self) -> None:
        b, _, a = make_received_batch(self.svc, qty=3)
        judge_plan(self.svc, b["id"], "不合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        # 先退 1 件 → 部分放行（部分已处置）
        b = self.svc.reject_units(b["id"], ids[:1], actor=ACTOR_QE)
        self.assertEqual(b["status"], BATCH_PARTIAL)
        # 复检可从部分放行发起
        b = self.svc.request_reinspection(b["id"], "整改复检", actor=ACTOR_QE)
        self.assertEqual(b["status"], BATCH_REINSPECTING)
        judge_plan(self.svc, b["id"], "合格")
        b = self.svc.release_units(b["id"], ids[1:], a["id"], actor=ACTOR_KEEPER)
        # 1 退 2 放的混合终态仍是部分放行
        self.assertEqual(b["status"], BATCH_PARTIAL)
        summary = b["decision_summary"]
        self.assertEqual(summary.get(D_REJECT), 1)
        self.assertEqual(summary.get(D_ACCEPT), 2)

    def test_reinspection_without_failed_plan_rejected(self) -> None:
        b, _, _ = make_received_batch(self.svc, qty=2)
        judge_plan(self.svc, b["id"], "合格")
        with self.assertRaises(DomainError):
            self.svc.request_reinspection(b["id"], "无不合格依据", actor=ACTOR_QE)

    def test_release_before_judgement_rejected(self) -> None:
        b, _, a = make_received_batch(self.svc, qty=2)
        ids = unit_ids(self.svc.get_batch(b["id"]))
        with self.assertRaises(DomainError):
            self.svc.release_units(b["id"], ids, a["id"], actor=ACTOR_KEEPER)
        # 单位保持隔离
        u = self.svc.trace_unit(ids[0])
        self.assertEqual(u["status"], STK_QUARANTINE)

    def test_reject_when_latest_round_passes_rejected(self) -> None:
        b, _, _ = make_received_batch(self.svc, qty=3)
        judge_plan(self.svc, b["id"], "不合格")
        self.svc.request_reinspection(b["id"], "复检", actor=ACTOR_QE)
        judge_plan(self.svc, b["id"], "合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        # 最新一轮合格，不能按最新结论退货
        with self.assertRaises(DomainError):
            self.svc.reject_units(b["id"], ids[:1], actor=ACTOR_QE)


if __name__ == "__main__":
    unittest.main()
