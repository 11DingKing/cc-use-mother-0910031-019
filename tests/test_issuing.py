"""领料出库：状态约束、FIFO、幂等与并发安全。"""
from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ServiceTestCase
from iqc import InsufficientStockError, StateError


class IssueTest(ServiceTestCase):
    def test_issue_only_from_available(self):
        self.receive("L-601", 100)  # 仍在隔离，未检验
        with self.assertRaises(InsufficientStockError):
            self.issuing.issue(part_no="P-100", qty=1, actor="计划员")

    def test_issue_partial_then_exhaust(self):
        self.released_lot("L-602", 100)
        first = self.issuing.issue(part_no="P-100", qty=30, actor="计划员")
        self.assertEqual(first["issue"]["qty"], 30)
        units = self.queries.list_units("L-602")
        self.assertEqual(units[0]["qty"], 70)
        self.assertEqual(units[0]["status"], "AVAILABLE")
        self.assertEqual(self.location("A-1")["occupied"], 70)
        self.issuing.issue(part_no="P-100", qty=70, actor="计划员")
        units = self.queries.list_units("L-602")
        self.assertEqual(units[0]["status"], "ISSUED")
        self.assertEqual(self.location("A-1")["occupied"], 0)
        with self.assertRaises(InsufficientStockError):
            self.issuing.issue(part_no="P-100", qty=1, actor="计划员")

    def test_issue_insufficient_leaves_no_trace(self):
        self.released_lot("L-603", 40)
        with self.assertRaises(InsufficientStockError):
            self.issuing.issue(part_no="P-100", qty=50, actor="计划员")
        units = self.queries.list_units("L-603")
        self.assertEqual(units[0]["qty"], 40)
        self.assertEqual(self.location("A-1")["occupied"], 40)
        self.assertEqual(self.table_count("issues"), 0)
        self.assertEqual(self.table_count("issue_lines"), 0)

    def test_issue_idempotent_by_request_id(self):
        self.released_lot("L-604", 100)
        first = self.issuing.issue(part_no="P-100", qty=10, actor="计划员", request_id="ISS-1")
        second = self.issuing.issue(part_no="P-100", qty=10, actor="计划员", request_id="ISS-1")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["issue"]["id"], second["issue"]["id"])
        self.assertEqual(self.location("A-1")["occupied"], 90)

    def test_issue_fifo_across_lots(self):
        self.released_lot("L-605", 60)
        self.released_lot("L-606", 60)
        result = self.issuing.issue(part_no="P-100", qty=100, actor="计划员")
        self.assertEqual([line["lot_no"] for line in result["lines"]], ["L-605", "L-606"])
        qtys = {line["lot_no"]: line["qty"] for line in result["lines"]}
        self.assertEqual(qtys, {"L-605": 60, "L-606": 40})

    def test_issue_scoped_to_lot(self):
        self.released_lot("L-607", 50)
        self.released_lot("L-608", 50)
        result = self.issuing.issue(part_no="P-100", qty=20, actor="计划员", lot_no="L-608")
        self.assertEqual(result["lines"][0]["lot_no"], "L-608")
        self.assertEqual(self.queries.get_lot("L-607")["stock"]["AVAILABLE"], 50)


class ConcurrencyTest(ServiceTestCase):
    def test_concurrent_issues_never_oversell(self):
        """10 个并发领料各要 20，库存 100：恰好 5 笔成功，不超发、不负库存。"""
        self.released_lot("L-701", 100)
        issued, rejected = [], []

        def worker(i):
            try:
                result = self.issuing.issue(part_no="P-100", qty=20,
                                            actor=f"计划员-{i}", request_id=f"ISS-{i}")
                issued.append(result["issue"]["qty"])
            except InsufficientStockError:
                rejected.append(i)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(10)))
        self.assertEqual(len(issued), 5)
        self.assertEqual(sum(issued), 100)
        self.assertEqual(len(rejected), 5)
        self.assertEqual(self.location("A-1")["occupied"], 0)
        stock = self.queries.stock_summary(part_no="P-100")
        self.assertEqual(stock[0]["AVAILABLE"], 0)
        self.assertEqual(stock[0]["ISSUED"], 100)

    def test_concurrent_full_release_only_one_wins(self):
        """并发整批放行：只有一笔生效，库存只上架一次。"""
        self.to_disposition("L-702", 100, defectives=(0, 0))
        outcomes = []

        def worker(_):
            try:
                self.disposition.release(lot_no="L-702", actor="质量工程师", reason="放行",
                                         target_location="A-1")
                outcomes.append("ok")
            except StateError:
                outcomes.append("state")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("state"), 3)
        self.assertEqual(self.location("A-1")["occupied"], 100)
        self.assertEqual(self.location("Q-1")["occupied"], 0)
        units = self.queries.list_units("L-702")
        self.assertEqual(sum(u["qty"] for u in units), 100)
        self.assertEqual(self.table_count("decisions"), 1)


if __name__ == "__main__":
    unittest.main()
