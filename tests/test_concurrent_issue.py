"""并发领料：同一可用单位只能被一张工单成功预留。"""
from __future__ import annotations

import threading
import unittest

from scenario import (
    ACTOR_KEEPER, ACTOR_QE, judge_plan, make_received_batch, make_service, unit_ids,
)
from incoming_inspection.models import (
    STK_AVAILABLE, STK_ISSUED, STK_RESERVED, DomainError,
)


class ConcurrentReservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        b, _, a = make_received_batch(self.svc, qty=1)
        judge_plan(self.svc, b["id"], "合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        b = self.svc.release_units(b["id"], ids, a["id"], actor=ACTOR_KEEPER)
        self.batch = b
        self.unit_id = ids[0]

    def test_only_one_winner_under_concurrency(self) -> None:
        outcomes: list[tuple[str, str]] = []
        lock = threading.Lock()

        def grab(worker: str) -> None:
            try:
                r = self.svc.create_reservation(self.unit_id, f"WO-{worker}",
                                                actor=ACTOR_KEEPER)
                with lock:
                    outcomes.append(("ok", r["id"]))
            except DomainError as exc:
                with lock:
                    outcomes.append(("fail", str(exc)))

        threads = [threading.Thread(target=grab, args=(f"{i:02d}",))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [o for o in outcomes if o[0] == "ok"]
        self.assertEqual(len(winners), 1, outcomes)
        u = self.svc.trace_unit(self.unit_id)
        self.assertEqual(u["status"], STK_RESERVED)
        active = [r for r in u["reservations"] if r["status"] == "已预留"]
        self.assertEqual(len(active), 1)

    def test_issue_then_terminal_constraints(self) -> None:
        r = self.svc.create_reservation(self.unit_id, "WO-1", actor=ACTOR_KEEPER)
        issued = self.svc.issue_reservation(r["id"], actor=ACTOR_KEEPER)
        self.assertEqual(issued["status"], "已领料")
        u = self.svc.trace_unit(self.unit_id)
        self.assertEqual(u["status"], STK_ISSUED)
        for op in (
            lambda: self.svc.issue_reservation(r["id"], actor=ACTOR_KEEPER),
            lambda: self.svc.cancel_reservation(r["id"], actor=ACTOR_KEEPER),
            lambda: self.svc.create_reservation(self.unit_id, "WO-2",
                                                actor=ACTOR_KEEPER),
        ):
            with self.assertRaises(DomainError):
                op()

    def test_cancel_returns_unit_to_available_and_rebook(self) -> None:
        r1 = self.svc.create_reservation(self.unit_id, "WO-1", actor=ACTOR_KEEPER)
        # 已预留给 WO-1 时，WO-2 不能抢占
        with self.assertRaises(DomainError):
            self.svc.create_reservation(self.unit_id, "WO-2", actor=ACTOR_KEEPER)
        self.svc.cancel_reservation(r1["id"], actor=ACTOR_KEEPER)
        u = self.svc.trace_unit(self.unit_id)
        self.assertEqual(u["status"], STK_AVAILABLE)
        # 释放后 WO-2 可成功预留
        r2 = self.svc.create_reservation(self.unit_id, "WO-2", actor=ACTOR_KEEPER)
        self.assertEqual(r2["work_order"], "WO-2")
        self.assertEqual(self.svc.trace_unit(self.unit_id)["status"], STK_RESERVED)

    def test_cannot_reserve_quarantined_unit(self) -> None:
        svc = make_service()
        b, _, _ = make_received_batch(svc, qty=1)
        ids = unit_ids(svc.get_batch(b["id"]))
        with self.assertRaises(DomainError):
            svc.create_reservation(ids[0], "WO-X", actor=ACTOR_KEEPER)


if __name__ == "__main__":
    unittest.main()
