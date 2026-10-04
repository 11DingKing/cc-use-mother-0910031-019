"""放行/退货的原子性与隔离库存边界。"""
from __future__ import annotations

import unittest

from scenario import ACTOR_KEEPER, ACTOR_QE, judge_plan, make_received_batch, make_service, unit_ids
from incoming_inspection.models import (
    BATCH_RECEIVED, D_ACCEPT, D_PENDING, STK_AVAILABLE, STK_QUARANTINE, DomainError,
)


class ReleaseAtomicityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def _passed_batch(self, qty: int = 3, accept_capacity: int | None = 1000):
        b, q, a = make_received_batch(self.svc, qty=qty,
                                      accept_capacity=accept_capacity)
        judge_plan(self.svc, b["id"], "合格")
        return self.svc.get_batch(b["id"]), a

    def test_release_moves_units_to_available_location(self) -> None:
        b, a = self._passed_batch(qty=3)
        ids = unit_ids(b)
        b = self.svc.release_units(b["id"], ids[:2], a["id"], actor=ACTOR_KEEPER)
        self.assertEqual(b["status"], "部分放行")
        for uid in ids[:2]:
            u = self.svc.trace_unit(uid)
            self.assertEqual(u["status"], STK_AVAILABLE)
            self.assertEqual(u["decision"], D_ACCEPT)
            self.assertEqual(u["location_code"], a["code"])
        # 未放行单位仍隔离待判 —— 生产侧不可领用
        u = self.svc.trace_unit(ids[2])
        self.assertEqual(u["status"], STK_QUARANTINE)
        self.assertEqual(u["decision"], D_PENDING)

    def test_failed_release_leaves_no_half_putaway(self) -> None:
        # 合格库位容量 1：放行 2 件必须整体失败
        b, a = self._passed_batch(qty=2, accept_capacity=1)
        ids = unit_ids(b)
        with self.assertRaises(DomainError):
            self.svc.release_units(b["id"], ids, a["id"], actor=ACTOR_KEEPER)
        for uid in ids:
            u = self.svc.trace_unit(uid)
            self.assertEqual(u["status"], STK_QUARANTINE)
            self.assertEqual(u["decision"], D_PENDING)
            self.assertEqual(u["movements"], [])
            self.assertEqual(u["decisions"], [])
        self.assertEqual(self.svc.get_batch(b["id"])["status"], BATCH_RECEIVED)

    def test_cannot_release_same_unit_twice(self) -> None:
        b, a = self._passed_batch(qty=2)
        ids = unit_ids(b)
        self.svc.release_units(b["id"], [ids[0]], a["id"], actor=ACTOR_KEEPER)
        with self.assertRaises(DomainError):
            self.svc.release_units(b["id"], [ids[0]], a["id"], actor=ACTOR_KEEPER)

    def test_release_rejects_non_accept_zone(self) -> None:
        b, q, _ = make_received_batch(self.svc, qty=2)
        judge_plan(self.svc, b["id"], "合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        with self.assertRaises(DomainError):
            self.svc.release_units(b["id"], ids, q["id"], actor=ACTOR_KEEPER)

    def test_reject_requires_failed_judgement(self) -> None:
        b, _, _ = make_received_batch(self.svc, qty=2)
        judge_plan(self.svc, b["id"], "合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        with self.assertRaises(DomainError):
            self.svc.reject_units(b["id"], ids, actor=ACTOR_QE)
        for uid in ids:
            u = self.svc.trace_unit(uid)
            self.assertEqual(u["status"], STK_QUARANTINE)
            self.assertEqual(u["decision"], D_PENDING)

    def test_partial_reject_then_release_mix(self) -> None:
        b, _, a = make_received_batch(self.svc, qty=4)
        judge_plan(self.svc, b["id"], "不合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        b = self.svc.reject_units(b["id"], ids[:2], reason="缺陷", actor=ACTOR_QE)
        self.assertEqual(b["status"], "部分放行")
        # 剩余单位不能在未复检/未让步情况下合格放行
        with self.assertRaises(DomainError):
            self.svc.release_units(b["id"], ids[2:], a["id"], actor=ACTOR_KEEPER)
        # 复检合格后放行剩余
        self.svc.request_reinspection(b["id"], "复检", actor=ACTOR_QE)
        judge_plan(self.svc, b["id"], "合格")
        b = self.svc.release_units(b["id"], ids[2:], a["id"], actor=ACTOR_KEEPER)
        self.assertEqual(b["status"], "部分放行")  # 退放混合
        self.assertEqual(b["decision_summary"].get("退货"), 2)
        self.assertEqual(b["decision_summary"].get(D_ACCEPT), 2)

    def test_decision_is_immutable(self) -> None:
        b, a = self._passed_batch(qty=1)
        ids = unit_ids(b)
        self.svc.release_units(b["id"], ids, a["id"], actor=ACTOR_KEEPER)
        # 已放行单位不能退货
        with self.assertRaises(DomainError):
            self.svc.reject_units(b["id"], ids, actor=ACTOR_QE)


if __name__ == "__main__":
    unittest.main()
