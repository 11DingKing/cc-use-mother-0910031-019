"""收货登记与抽样检验的状态约束测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ServiceTestCase
from iqc import CapacityError, DuplicateError, NotFoundError, StateError, ValidationError


class ReceivingTest(ServiceTestCase):
    def test_receive_lot_creates_quarantined_unit(self):
        lot = self.receive("L-001", 100)
        self.assertEqual(lot["status"], "RECEIVED")
        units = self.queries.list_units("L-001")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["status"], "QUARANTINED")
        self.assertEqual(units[0]["location_code"], "Q-1")
        self.assertEqual(units[0]["qty"], 100)
        self.assertEqual(self.location("Q-1")["occupied"], 100)

    def test_receive_rejects_unknown_part(self):
        with self.assertRaises(NotFoundError):
            self.receiving.receive_lot(lot_no="L-X", part_no="P-404", supplier="供应商",
                                       qty=1, actor="仓管员", quarantine_location="Q-1")

    def test_receive_requires_quarantine_location(self):
        with self.assertRaises(ValidationError):
            self.receive("L-002", 1, quarantine_location="A-1")

    def test_receive_duplicate_lot_rejected(self):
        self.receive("L-003", 10)
        with self.assertRaises(DuplicateError):
            self.receive("L-003", 10)

    def test_receive_with_explicit_units(self):
        self.receive("L-004", 60, units=[{"code": "BOX-1", "qty": 20},
                                         {"code": "BOX-2", "qty": 40}])
        units = self.queries.list_units("L-004")
        self.assertEqual([u["unit_code"] for u in units], ["BOX-1", "BOX-2"])
        with self.assertRaises(ValidationError):
            self.receive("L-005", 50, units=[{"code": "B-1", "qty": 40}])

    def test_receive_rolls_back_when_unit_code_duplicated(self):
        # 第二个库存单位编码重复 → 整体回滚，不留批次与库位占用
        with self.assertRaises(DuplicateError):
            self.receive("L-006", 100, units=[{"code": "DUP", "qty": 50},
                                              {"code": "DUP", "qty": 50}])
        with self.assertRaises(NotFoundError):
            self.queries.get_lot("L-006")
        self.assertEqual(self.location("Q-1")["occupied"], 0)

    def test_quarantine_capacity_enforced_atomically(self):
        self.receiving.create_location(code="Q-S", kind="QUARANTINE", capacity=10)
        with self.assertRaises(CapacityError):
            self.receive("L-007", 11, quarantine_location="Q-S")
        with self.assertRaises(NotFoundError):
            self.queries.get_lot("L-007")
        self.assertEqual(self.location("Q-S")["occupied"], 0)


class InspectionTest(ServiceTestCase):
    def test_plan_state_constraints(self):
        self.receive("L-101", 50)
        plan = self.start_plan("L-101")
        self.assertEqual(plan["round"], 1)
        self.assertEqual(len(plan["items"]), 2)
        with self.assertRaises(StateError):  # 已有进行中的计划
            self.start_plan("L-101")

    def test_plan_rejected_after_disposition_stage(self):
        self.to_disposition("L-102", 50, defectives=(0, 0))
        with self.assertRaises(StateError):
            self.start_plan("L-102")

    def test_plan_validates_ac_re_pair(self):
        self.receive("L-103", 50)
        with self.assertRaises(ValidationError):
            self.inspection.create_plan(
                lot_no="L-103", actor="质量工程师", standard="GB/T 2828.1", level="II",
                aql="1.0", sample_size=10, accept_qty=2, reject_qty=5,
                items=[{"name": "外观", "requirement": "表面无划伤"}])

    def test_plan_rejects_sample_larger_than_stock(self):
        self.receive("L-104", 10)
        with self.assertRaises(ValidationError):
            self.start_plan("L-104", sample_size=20)

    def test_item_result_rules(self):
        self.receive("L-105", 50)
        plan = self.start_plan("L-105")
        item = plan["items"][0]
        with self.assertRaises(ValidationError):  # FAIL 必须有不合格数
            self.inspection.record_item_result(lot_no="L-105", item_id=item["id"],
                                               result="FAIL", defective_qty=0, actor="质检员")
        with self.assertRaises(ValidationError):  # PASS 不能带不合格数
            self.inspection.record_item_result(lot_no="L-105", item_id=item["id"],
                                               result="PASS", defective_qty=1, actor="质检员")
        self.inspection.record_item_result(lot_no="L-105", item_id=item["id"],
                                           result="PASS", defective_qty=0, actor="质检员")
        with self.assertRaises(StateError):  # 不能重复记录
            self.inspection.record_item_result(lot_no="L-105", item_id=item["id"],
                                               result="PASS", defective_qty=0, actor="质检员")

    def test_record_item_requires_plan(self):
        self.receive("L-106", 50)
        with self.assertRaises(NotFoundError):
            self.inspection.record_item_result(lot_no="L-106", item_id=999,
                                               result="PASS", actor="质检员")

    def test_complete_requires_all_items(self):
        self.receive("L-107", 50)
        plan = self.start_plan("L-107")
        self.inspection.record_item_result(lot_no="L-107", item_id=plan["items"][0]["id"],
                                           result="PASS", actor="质检员")
        with self.assertRaises(StateError):
            self.complete("L-107")

    def test_verdict_pass_and_fail(self):
        verdict = self.to_disposition("L-108", 100, defectives=(1, 1))  # 2 <= Ac=2
        self.assertEqual(verdict["result"], "PASS")
        verdict = self.to_disposition("L-109", 100, defectives=(2, 2))  # 4 > Ac=2
        self.assertEqual(verdict["result"], "FAIL")
        lot = self.queries.get_lot("L-109")
        self.assertEqual(lot["status"], "AWAITING_DISPOSITION")
        self.assertEqual(lot["stock"]["QUARANTINED"], 100)

    def test_defect_evidence_linked_to_item(self):
        self.receive("L-110", 50)
        plan = self.start_plan("L-110")
        self.inspect(plan, [3, 0], "L-110")
        trace = self.queries.trace_lot("L-110")
        item_defects = trace["plans"][0]["items"][0]["defects"]
        self.assertEqual(len(item_defects), 1)
        self.assertEqual(item_defects[0]["defect_code"], "SCRATCH")
        self.assertEqual(item_defects[0]["severity"], "MAJOR")
        self.assertEqual(item_defects[0]["evidence_uri"], "photos/scratch.jpg")

    def test_add_defect_after_inspection_completed(self):
        self.to_disposition("L-111", 50, defectives=(0, 0))
        defect = self.inspection.add_defect(lot_no="L-111", defect_code="RUST",
                                            severity="MINOR", qty=1,
                                            description="复检发现轻微锈点", actor="质量工程师")
        self.assertEqual(defect["lot_no"], "L-111")
        trace = self.queries.trace_lot("L-111")
        self.assertEqual(len(trace["lot_defects"]), 1)


if __name__ == "__main__":
    unittest.main()
