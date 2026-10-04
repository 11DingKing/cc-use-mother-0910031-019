"""处置决定：放行原子性、部分放行、让步审批、复检与退货。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ServiceTestCase
from iqc import CapacityError, StateError, ValidationError


class ReleaseTest(ServiceTestCase):
    def test_release_blocked_before_inspection(self):
        self.receive("L-201", 100)
        with self.assertRaises(StateError):
            self.disposition.release(lot_no="L-201", actor="质量工程师", reason="放行",
                                     target_location="A-1")

    def test_full_release_moves_stock_atomically(self):
        self.to_disposition("L-202", 100, defectives=(0, 0))
        result = self.disposition.release(lot_no="L-202", actor="质量工程师-李雷",
                                          reason="检验合格放行", target_location="A-1")
        self.assertEqual(result["decision"]["dtype"], "RELEASE")
        self.assertEqual(result["lot"]["status"], "RELEASED")
        units = self.queries.list_units("L-202")
        self.assertEqual(units[0]["status"], "AVAILABLE")
        self.assertEqual(units[0]["location_code"], "A-1")
        self.assertEqual(units[0]["decision_id"], result["decision"]["id"])
        self.assertEqual(self.location("Q-1")["occupied"], 0)
        self.assertEqual(self.location("A-1")["occupied"], 100)

    def test_partial_release_splits_stock(self):
        self.to_disposition("L-203", 100, defectives=(0, 0))
        first = self.disposition.release(lot_no="L-203", qty=40, actor="质量工程师",
                                         reason="先放行 40", target_location="A-1")
        self.assertEqual(first["decision"]["dtype"], "PARTIAL_RELEASE")
        self.assertEqual(first["lot"]["status"], "PARTIALLY_RELEASED")
        units = self.queries.list_units("L-203")
        by_status = {u["status"]: u["qty"] for u in units}
        self.assertEqual(by_status["AVAILABLE"], 40)
        self.assertEqual(by_status["QUARANTINED"], 60)
        self.assertEqual(self.location("Q-1")["occupied"], 60)
        self.assertEqual(self.location("A-1")["occupied"], 40)
        second = self.disposition.release(lot_no="L-203", actor="质量工程师",
                                          reason="放行剩余", target_location="A-1")
        self.assertEqual(second["decision"]["dtype"], "RELEASE")
        self.assertEqual(second["lot"]["status"], "RELEASED")
        self.assertEqual(self.location("Q-1")["occupied"], 0)

    def test_release_capacity_failure_leaves_no_partial_state(self):
        """目标库位容量不足 → 决定、库存转移、库位占用全部回滚，不留半上架状态。"""
        self.receiving.create_location(code="A-S", kind="AVAILABLE", capacity=30)
        self.to_disposition("L-204", 100, defectives=(0, 0))
        events_before = self.table_count("events")
        with self.assertRaises(CapacityError):
            self.disposition.release(lot_no="L-204", actor="质量工程师", reason="放行",
                                     target_location="A-S")
        lot = self.queries.get_lot("L-204")
        self.assertEqual(lot["status"], "AWAITING_DISPOSITION")
        units = self.queries.list_units("L-204")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["status"], "QUARANTINED")
        self.assertEqual(units[0]["qty"], 100)
        self.assertIsNone(units[0]["decision_id"])
        self.assertEqual(self.location("Q-1")["occupied"], 100)
        self.assertEqual(self.location("A-S")["occupied"], 0)
        trace = self.queries.trace_lot("L-204")
        self.assertEqual(trace["decisions"], [])
        self.assertEqual(self.table_count("events"), events_before)  # 无新增事件

    def test_release_qty_exceeds_remaining(self):
        self.to_disposition("L-205", 50, defectives=(0, 0))
        with self.assertRaises(ValidationError):
            self.disposition.release(lot_no="L-205", qty=60, actor="质量工程师",
                                     reason="超量放行", target_location="A-1")
        self.assertEqual(self.location("A-1")["occupied"], 0)

    def test_release_idempotent_by_request_id(self):
        self.to_disposition("L-206", 100, defectives=(0, 0))
        first = self.disposition.release(lot_no="L-206", actor="质量工程师", reason="放行",
                                         target_location="A-1", request_id="REQ-1")
        second = self.disposition.release(lot_no="L-206", actor="质量工程师", reason="放行",
                                          target_location="A-1", request_id="REQ-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["decision"]["id"], second["decision"]["id"])
        self.assertEqual(self.location("A-1")["occupied"], 100)  # 只上架一次


class ConcessionTest(ServiceTestCase):
    def test_failed_verdict_requires_concession(self):
        self.to_disposition("L-301", 100, defectives=(3, 1))  # 4 > Ac=2 → FAIL
        with self.assertRaises(StateError):
            self.disposition.release(lot_no="L-301", actor="质量工程师", reason="直接放行",
                                     target_location="A-1")
        self.assertEqual(self.location("Q-1")["occupied"], 100)
        self.assertEqual(self.location("A-1")["occupied"], 0)

    def test_concession_approval_flow(self):
        self.to_disposition("L-302", 100, defectives=(3, 0))
        concession = self.disposition.request_concession(
            lot_no="L-302", qty=100, reason="客户急需，让步接收", actor="质量工程师-李雷")
        self.assertEqual(concession["status"], "PENDING")
        with self.assertRaises(StateError):  # 审批人不能与申请人相同
            self.disposition.approve_concession(concession_id=concession["id"],
                                                actor="质量工程师-李雷")
        approved = self.disposition.approve_concession(concession_id=concession["id"],
                                                       actor="质量经理-韩梅", note="同意")
        self.assertEqual(approved["status"], "APPROVED")
        result = self.disposition.release(lot_no="L-302", actor="质量工程师-李雷",
                                          reason="让步放行", target_location="A-1",
                                          concession_id=concession["id"])
        self.assertEqual(result["decision"]["dtype"], "CONCESSION_RELEASE")
        self.assertEqual(result["lot"]["status"], "RELEASED")
        trace = self.queries.trace_lot("L-302")
        unit = trace["units"][0]
        self.assertEqual(unit["decision"]["dtype"], "CONCESSION_RELEASE")
        self.assertEqual(unit["decision"]["concession_id"], concession["id"])
        self.assertEqual(unit["concession"]["decided_by"], "质量经理-韩梅")
        after = self.queries.get_concession(concession["id"])
        self.assertEqual(after["status"], "CONSUMED")

    def test_concession_rejection_blocks_release(self):
        self.to_disposition("L-303", 100, defectives=(3, 0))
        concession = self.disposition.request_concession(lot_no="L-303", qty=100,
                                                         reason="让步申请", actor="李雷")
        rejected = self.disposition.reject_concession(concession_id=concession["id"],
                                                      actor="韩梅", note="风险不可接受")
        self.assertEqual(rejected["status"], "REJECTED")
        with self.assertRaises(StateError):
            self.disposition.release(lot_no="L-303", actor="李雷", reason="让步放行",
                                     target_location="A-1", concession_id=concession["id"])

    def test_concession_qty_limit(self):
        self.to_disposition("L-304", 100, defectives=(3, 0))
        concession = self.disposition.request_concession(lot_no="L-304", qty=30,
                                                         reason="让步 30 件", actor="李雷")
        self.disposition.approve_concession(concession_id=concession["id"], actor="韩梅")
        with self.assertRaises(StateError):
            self.disposition.release(lot_no="L-304", qty=40, actor="李雷", reason="超量",
                                     target_location="A-1", concession_id=concession["id"])
        ok = self.disposition.release(lot_no="L-304", qty=30, actor="李雷", reason="量内放行",
                                      target_location="A-1", concession_id=concession["id"])
        self.assertEqual(ok["lot"]["status"], "PARTIALLY_RELEASED")
        self.assertEqual(self.queries.get_concession(concession["id"])["status"], "CONSUMED")

    def test_passed_lot_rejects_concession(self):
        self.to_disposition("L-305", 100, defectives=(0, 0))
        with self.assertRaises(ValidationError):
            self.disposition.release(lot_no="L-305", actor="李雷", reason="误带让步",
                                     target_location="A-1", concession_id=1)


class ReinspectReturnTest(ServiceTestCase):
    def test_reinspect_then_release(self):
        self.to_disposition("L-401", 100, defectives=(3, 0))  # FAIL
        result = self.disposition.request_reinspect(lot_no="L-401", actor="质量工程师",
                                                    reason="供应商异议，安排复检")
        self.assertEqual(result["decision"]["dtype"], "REINSPECT")
        self.assertEqual(result["lot"]["status"], "INSPECTING")
        plan2 = self.start_plan("L-401")
        self.assertEqual(plan2["round"], 2)
        self.inspect(plan2, [0, 0], "L-401")
        verdict = self.complete("L-401")
        self.assertEqual(verdict["result"], "PASS")
        released = self.disposition.release(lot_no="L-401", actor="质量工程师",
                                            reason="复检合格放行", target_location="A-1")
        self.assertEqual(released["lot"]["status"], "RELEASED")
        trace = self.queries.trace_lot("L-401")
        self.assertEqual([p["round"] for p in trace["plans"]], [1, 2])
        self.assertEqual([d["dtype"] for d in trace["decisions"]], ["REINSPECT", "RELEASE"])

    def test_reinspect_requires_disposition_state(self):
        self.receive("L-402", 50)
        with self.assertRaises(StateError):
            self.disposition.request_reinspect(lot_no="L-402", actor="质量工程师",
                                               reason="尚未检验")

    def test_return_to_supplier(self):
        self.to_disposition("L-403", 100, defectives=(3, 1))  # FAIL
        result = self.disposition.return_to_supplier(lot_no="L-403", actor="质量工程师",
                                                     reason="整批退货")
        self.assertEqual(result["decision"]["dtype"], "RETURN")
        self.assertEqual(result["lot"]["status"], "RETURNED")
        units = self.queries.list_units("L-403")
        self.assertEqual(units[0]["status"], "RETURNED")
        self.assertEqual(self.location("Q-1")["occupied"], 0)

    def test_mixed_release_and_return_closes_lot(self):
        self.to_disposition("L-404", 100, defectives=(0, 0))
        self.disposition.release(lot_no="L-404", qty=70, actor="质量工程师",
                                 reason="放行 70", target_location="A-1")
        result = self.disposition.return_to_supplier(lot_no="L-404", actor="质量工程师",
                                                     reason="剩余退货")
        self.assertEqual(result["decision"]["qty"], 30)
        self.assertEqual(result["lot"]["status"], "CLOSED")
        self.assertEqual(self.location("Q-1")["occupied"], 0)
        self.assertEqual(self.location("A-1")["occupied"], 70)


class TraceTest(ServiceTestCase):
    def test_trace_shows_decision_basis_per_unit(self):
        self.to_disposition("L-501", 100, defectives=(1, 0))
        self.disposition.release(lot_no="L-501", qty=60, actor="质量工程师-李雷",
                                 reason="先放行 60", target_location="A-1")
        self.issuing.issue(part_no="P-100", qty=20, actor="计划员-赵六")
        self.disposition.return_to_supplier(lot_no="L-501", actor="质量工程师-李雷",
                                            reason="剩余退货")
        trace = self.queries.trace_lot("L-501")
        self.assertEqual(trace["lot"]["status"], "CLOSED")
        self.assertEqual(trace["lot"]["stock"],
                         {"QUARANTINED": 0, "AVAILABLE": 40, "ISSUED": 20, "RETURNED": 40})
        units = {u["unit_code"]: u for u in trace["units"]}
        released = units["L-501-U002"]
        self.assertEqual(released["status"], "AVAILABLE")
        self.assertEqual(released["qty"], 40)
        self.assertEqual(released["decision"]["dtype"], "PARTIAL_RELEASE")
        self.assertEqual(released["decision"]["decided_by"], "质量工程师-李雷")
        self.assertEqual(released["issues"][0]["qty"], 20)
        self.assertEqual(released["issues"][0]["requested_by"], "计划员-赵六")
        returned = units["L-501-U001"]
        self.assertEqual(returned["status"], "RETURNED")
        self.assertEqual(returned["decision"]["dtype"], "RETURN")
        # 抽样计划、检验项与缺陷证据一并可追溯
        self.assertEqual(trace["plans"][0]["items"][0]["defective_qty"], 1)
        self.assertEqual(trace["plans"][0]["items"][0]["defects"][0]["defect_code"], "SCRATCH")
        actions = [e["action"] for e in trace["events"]]
        self.assertIn("DECISION_MADE", actions)
        self.assertIn("UNIT_MOVED", actions)


if __name__ == "__main__":
    unittest.main()
