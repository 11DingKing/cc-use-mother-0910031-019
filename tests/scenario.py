"""测试公用：临时数据库与标准检验场景构造。"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from incoming_inspection.services import Service  # noqa: E402
from incoming_inspection.store import Database  # noqa: E402

ACTOR_PLANNER = "采购计划员"
ACTOR_QE = "质量工程师"
ACTOR_KEEPER = "仓储管理员"


def make_service() -> Service:
    tmp = tempfile.mkdtemp(prefix="iqi-")
    db = Database(os.path.join(tmp, "incoming.sqlite3"))
    db.initialize()
    return Service(db)


def make_received_batch(svc: Service, qty: int = 5,
                        quarantine_capacity: int | None = 1000,
                        accept_capacity: int | None = 1000):
    """建立库位、批次并完成到货确认，返回 (batch, quarantine_loc, accept_loc)。"""
    q = svc.create_location("Q-" + uuid.uuid4().hex[:6], "隔离", quarantine_capacity)
    a = svc.create_location("A-" + uuid.uuid4().hex[:6], "合格", accept_capacity)
    b = svc.create_batch("B-" + uuid.uuid4().hex[:10], "P-001", "六角螺栓",
                         "供应商甲", qty, actor=ACTOR_PLANNER)
    b = svc.confirm_arrival(b["id"], q["id"], actor=ACTOR_KEEPER)
    return b, q, a


def judge_plan(svc: Service, batch_id: str, verdict: str,
               sample_size: int = 2, actor: str = ACTOR_QE) -> dict:
    """创建一轮抽样计划、录入检验项并完成判定。

    合格：全部检验项合格；不合格：首项缺陷（附严重度证据）+ 其余合格。
    """
    plan = svc.create_sampling_plan(batch_id, sample_size, actor=actor)
    for seq in range(sample_size):
        item = svc.add_inspection_item(plan["id"], f"检验项{seq + 1}",
                                       spec_requirement="图样要求")
        if verdict == "不合格" and seq == 0:
            svc.record_inspection(
                item["id"], "缺陷", measured_value="超差 0.3",
                evidences=[{"is_defect": True, "severity": "严重",
                            "evidence_ref": "EV-" + uuid.uuid4().hex[:6],
                            "description": "尺寸超差"}],
                actor=actor)
        else:
            svc.record_inspection(item["id"], "合格", actor=actor)
    return svc.judge_sampling(plan["id"], verdict, actor=actor)


def unit_ids(batch: dict) -> list[str]:
    return [u["id"] for u in batch["units"]]
