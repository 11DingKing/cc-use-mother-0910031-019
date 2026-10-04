"""让步审批状态约束。"""
from __future__ import annotations

import unittest

from scenario import ACTOR_KEEPER, ACTOR_QE, judge_plan, make_received_batch, make_service, unit_ids
from incoming_inspection.models import (
    CONCESSION_APPROVED, CONCESSION_PENDING, CONCESSION_REJECTED,
    D_CONCESSION, STK_AVAILABLE, DomainError,
)


class ConcessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        b, _, a = make_received_batch(self.svc, qty=3)
        judge_plan(self.svc, b["id"], "不合格")
        self.batch = self.svc.get_batch(b["id"])
        self.accept_loc = a
        self.ids = unit_ids(self.batch)

    def test_concession_requires_judged_plan(self) -> None:
        svc = make_service()
        b, q, _ = make_received_batch(svc, qty=2)
        ids = unit_ids(svc.get_batch(b["id"]))
        with self.assertRaises(DomainError):
            svc.request_concession(ids[0], "未检先让步", actor=ACTOR_QE)

    def test_pending_concession_cannot_release(self) -> None:
        c = self.svc.request_concession(self.ids[0], "轻微外观问题",
                                        proposed_use="内部工单", actor=ACTOR_QE)
        self.assertEqual(c["status"], CONCESSION_PENDING)
        with self.assertRaises(DomainError):
            self.svc.release_units(
                self.batch["id"], [self.ids[0]], self.accept_loc["id"],
                decision=D_CONCESSION,
                concession_ids={self.ids[0]: c["id"]}, actor=ACTOR_KEEPER)

    def test_rejected_concession_cannot_release(self) -> None:
        c = self.svc.request_concession(self.ids[0], "理由", actor=ACTOR_QE)
        self.svc.decide_concession(c["id"], False, decision_note="风险过大",
                                   actor=ACTOR_QE)
        with self.assertRaises(DomainError):
            self.svc.release_units(
                self.batch["id"], [self.ids[0]], self.accept_loc["id"],
                decision=D_CONCESSION,
                concession_ids={self.ids[0]: c["id"]}, actor=ACTOR_KEEPER)

    def test_approved_concession_release_and_immutable_decision(self) -> None:
        c = self.svc.request_concession(self.ids[0], "尺寸偏上限可装配",
                                        actor=ACTOR_QE)
        decided = self.svc.decide_concession(c["id"], True,
                                             decision_note="限非关键面",
                                             actor=ACTOR_QE)
        self.assertEqual(decided["status"], CONCESSION_APPROVED)
        b = self.svc.release_units(
            self.batch["id"], [self.ids[0]], self.accept_loc["id"],
            decision=D_CONCESSION,
            concession_ids={self.ids[0]: c["id"]}, actor=ACTOR_KEEPER)
        self.assertEqual(b["status"], "部分放行")
        u = self.svc.trace_unit(self.ids[0])
        self.assertEqual(u["status"], STK_AVAILABLE)
        self.assertEqual(u["decision"], D_CONCESSION)
        # 审批结论不可更改
        with self.assertRaises(DomainError):
            self.svc.decide_concession(c["id"], False, actor=ACTOR_QE)

    def test_concession_must_match_unit(self) -> None:
        c1 = self.svc.request_concession(self.ids[0], "理由", actor=ACTOR_QE)
        self.svc.decide_concession(c1["id"], True, actor=ACTOR_QE)
        with self.assertRaises(DomainError):
            self.svc.release_units(
                self.batch["id"], [self.ids[1]], self.accept_loc["id"],
                decision=D_CONCESSION,
                concession_ids={self.ids[1]: c1["id"]}, actor=ACTOR_KEEPER)

    def test_only_one_open_concession_per_unit(self) -> None:
        self.svc.request_concession(self.ids[0], "第一次", actor=ACTOR_QE)
        with self.assertRaises(DomainError):
            self.svc.request_concession(self.ids[0], "重复申请", actor=ACTOR_QE)
        # 驳回后可以重新申请
        cs = self.svc.trace_unit(self.ids[0])["concessions"]
        self.svc.decide_concession(cs[0]["id"], False, actor=ACTOR_QE)
        c2 = self.svc.request_concession(self.ids[0], "整改后再申请", actor=ACTOR_QE)
        self.assertEqual(c2["status"], CONCESSION_PENDING)


if __name__ == "__main__":
    unittest.main()
