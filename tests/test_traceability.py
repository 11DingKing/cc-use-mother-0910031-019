"""决定依据追溯：沿批次/单位可还原每个决定的证据链。"""
from __future__ import annotations

import json
import unittest

from scenario import (
    ACTOR_KEEPER, ACTOR_QE, judge_plan, make_received_batch, make_service, unit_ids,
)


class TraceabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        b, _, a = make_received_batch(self.svc, qty=3)
        judge_plan(self.svc, b["id"], "不合格")
        ids = unit_ids(self.svc.get_batch(b["id"]))
        # 单位0 让步；单位1 退货；单位2 留待复检后合格放行
        c = self.svc.request_concession(ids[0], "外观轻微缺陷可返工",
                                        proposed_use="普通机型", actor=ACTOR_QE)
        self.svc.decide_concession(c["id"], True, decision_note="限普通机型",
                                   actor=ACTOR_QE)
        self.svc.release_units(b["id"], [ids[0]], a["id"], decision="让步接收",
                               concession_ids={ids[0]: c["id"]}, actor=ACTOR_KEEPER)
        self.svc.reject_units(b["id"], [ids[1]], reason="尺寸严重超差",
                              actor=ACTOR_QE)
        self.svc.request_reinspection(b["id"], "供应商整改", actor=ACTOR_QE)
        judge_plan(self.svc, b["id"], "合格", sample_size=2)
        self.svc.release_units(b["id"], [ids[2]], a["id"], actor=ACTOR_KEEPER)
        self.batch = self.svc.get_batch(b["id"])
        self.ids = ids

    def test_unit_trace_shows_decision_basis(self) -> None:
        # 让步单位
        u0 = self.svc.trace_unit(self.ids[0])
        self.assertEqual(len(u0["decisions"]), 1)
        d = u0["decisions"][0]
        self.assertEqual(d["to_decision"], "让步接收")
        self.assertEqual(d["basis_type"], "让步审批")
        self.assertEqual(d["basis"]["status"], "已批准")
        self.assertEqual(d["basis"]["decision_note"], "限普通机型")
        self.assertEqual([(m["from_status"], m["to_status"]) for m in u0["movements"]],
                         [("隔离", "可用")])

        # 退货单位：依据是第 1 轮不合格计划
        u1 = self.svc.trace_unit(self.ids[1])
        d1 = u1["decisions"][0]
        self.assertEqual(d1["to_decision"], "退货")
        self.assertEqual(d1["basis_type"], "抽样计划")
        self.assertEqual(d1["basis"]["round"], 1)
        self.assertEqual(d1["basis"]["result"], "不合格")
        self.assertEqual([(m["from_status"], m["to_status"]) for m in u1["movements"]],
                         [("隔离", "已退货")])

        # 复检放行单位：依据是第 2 轮合格计划
        u2 = self.svc.trace_unit(self.ids[2])
        d2 = u2["decisions"][0]
        self.assertEqual(d2["to_decision"], "合格放行")
        self.assertEqual(d2["basis"]["round"], 2)
        self.assertEqual(d2["basis"]["result"], "合格")

    def test_batch_trace_contains_all_rounds_and_units(self) -> None:
        tr = self.svc.trace_batch(self.batch["id"])
        self.assertEqual([p["round"] for p in tr["plans"]], [1, 2])
        self.assertEqual([p["result"] for p in tr["plans"]], ["不合格", "合格"])
        self.assertEqual(len(tr["trace_units"]), 3)
        # 第 1 轮的检验项保留缺陷证据描述
        first_round_items = tr["plans"][0]["items"]
        evidences = []
        for item in first_round_items:
            evidences.extend(json.loads(item["evidences"]))
        defects = [e for e in evidences if e["is_defect"]]
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["severity"], "严重")
        # 批次事件链非空且只增
        events = [e["event"] for e in tr["events"]]
        self.assertIn("batch.created", events)
        self.assertIn("batch.released", events)
        self.assertIn("batch.rejected", events)
        self.assertIn("batch.reinspect_requested", events)

    def test_defect_evidence_linked_to_judgement_basis(self) -> None:
        """端到端依据链：缺陷证据 → 检验项 → 计划判定 → 单位退货决定。"""
        tr = self.svc.trace_batch(self.batch["id"])
        rejected = next(u for u in tr["trace_units"]
                        if u["decision"] == "退货")
        basis_plan_id = rejected["decisions"][0]["basis_id"]
        plan = next(p for p in tr["plans"] if p["id"] == basis_plan_id)
        defect_items = [i for i in plan["items"] if i["result"] == "缺陷"]
        self.assertTrue(defect_items)
        linked = []
        for i in defect_items:
            linked.extend(e for e in json.loads(i["evidences"]) if e["is_defect"])
        self.assertTrue(linked)
        self.assertEqual(linked[0]["description"], "尺寸超差")


if __name__ == "__main__":
    unittest.main()
