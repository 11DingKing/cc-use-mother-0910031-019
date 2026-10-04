"""服务层测试公共装置。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iqc import (Database, DispositionService, InspectionService, IssuingService,
                 QueryService, ReceivingService)


class ServiceTestCase(unittest.TestCase):
    """每个用例一套独立的临时数据库与服务。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "iqc.db")
        self.receiving = ReceivingService(self.db)
        self.inspection = InspectionService(self.db)
        self.disposition = DispositionService(self.db)
        self.issuing = IssuingService(self.db)
        self.queries = QueryService(self.db)
        self.receiving.create_part(part_no="P-100", name="精密轴承", spec="GB/T 307")
        self.receiving.create_location(code="Q-1", kind="QUARANTINE", capacity=10000)
        self.receiving.create_location(code="A-1", kind="AVAILABLE", capacity=10000)

    # ------------------------------------------------------------- 流程装置
    def receive(self, lot_no="L-001", qty=100, **overrides):
        params = dict(lot_no=lot_no, part_no="P-100", supplier="供应商甲", qty=qty,
                      actor="仓管员-王五", quarantine_location="Q-1")
        params.update(overrides)
        return self.receiving.receive_lot(**params)

    def start_plan(self, lot_no="L-001", accept_qty=2, sample_size=20):
        return self.inspection.create_plan(
            lot_no=lot_no, actor="质量工程师-李雷", standard="GB/T 2828.1", level="II",
            aql="1.0", sample_size=sample_size, accept_qty=accept_qty,
            reject_qty=accept_qty + 1,
            items=[{"name": "外观", "requirement": "表面无划伤"},
                   {"name": "尺寸", "requirement": "符合图纸公差"}])

    def inspect(self, plan, defectives, lot_no="L-001"):
        for item, defective in zip(plan["items"], defectives):
            defects = []
            if defective:
                defects.append({"defect_code": "SCRATCH", "severity": "MAJOR",
                                "qty": defective, "description": "表面划伤",
                                "evidence_uri": "photos/scratch.jpg"})
            self.inspection.record_item_result(
                lot_no=lot_no, item_id=item["id"],
                result="FAIL" if defective else "PASS", defective_qty=defective,
                actor="质量工程师-李雷", defects=defects)

    def complete(self, lot_no="L-001"):
        return self.inspection.complete_inspection(lot_no=lot_no, actor="质量工程师-李雷")

    def to_disposition(self, lot_no="L-001", qty=100, defectives=(0, 0), accept_qty=2):
        """收货 → 抽样 → 检验 → 完成检验，返回到达待判定时的判定结果。"""
        self.receive(lot_no, qty)
        plan = self.start_plan(lot_no, accept_qty=accept_qty)
        self.inspect(plan, defectives, lot_no)
        return self.complete(lot_no)

    def released_lot(self, lot_no="L-001", qty=100):
        """检验合格并全部放行的批次。"""
        self.to_disposition(lot_no, qty, defectives=(0, 0))
        return self.disposition.release(lot_no=lot_no, actor="质量工程师-李雷",
                                        reason="检验合格放行", target_location="A-1")

    def location(self, code):
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM locations WHERE code = ?", (code,)).fetchone()
            return dict(row) if row else None

    def table_count(self, table):
        with self.db.read() as conn:
            return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
