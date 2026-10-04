"""抽样计划、检验项与缺陷证据约束。"""
from __future__ import annotations

import unittest

from scenario import ACTOR_QE, make_received_batch, make_service
from incoming_inspection.models import (
    PLAN_INSPECTING, PLAN_JUDGED, PLAN_PENDING, DomainError,
)


class SamplingAndInspectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        b, _, _ = make_received_batch(self.svc, qty=4)
        self.batch = b

    def test_plan_rounds_auto_increment(self) -> None:
        svc = self.svc
        p1 = svc.create_sampling_plan(self.batch["id"], 2, actor=ACTOR_QE)
        self.assertEqual(p1["round"], 1)
        self.assertEqual(p1["status"], PLAN_PENDING)
        # 复检计划必须先进入复检中
        with self.assertRaises(DomainError):
            svc.create_sampling_plan(self.batch["id"], 2, actor=ACTOR_QE)
        item = svc.add_inspection_item(p1["id"], "尺寸")
        svc.record_inspection(item["id"], "缺陷",
                              evidences=[{"is_defect": True, "severity": "轻微",
                                          "description": "毛刺"}],
                              actor=ACTOR_QE)
        item2 = svc.add_inspection_item(p1["id"], "外观")
        svc.record_inspection(item2["id"], "合格", actor=ACTOR_QE)
        svc.judge_sampling(p1["id"], "不合格", actor=ACTOR_QE)
        svc.request_reinspection(self.batch["id"], "复检", actor=ACTOR_QE)
        p2 = svc.create_sampling_plan(self.batch["id"], 2, actor=ACTOR_QE)
        self.assertEqual(p2["round"], 2)

    def test_recording_result_auto_starts_plan(self) -> None:
        svc = self.svc
        p = svc.create_sampling_plan(self.batch["id"], 1, actor=ACTOR_QE)
        item = svc.add_inspection_item(p["id"], "尺寸")
        svc.record_inspection(item["id"], "合格", actor=ACTOR_QE)
        self.assertEqual(svc.get_sampling_plan(p["id"])["status"], PLAN_INSPECTING)

    def test_defect_result_requires_defect_evidence(self) -> None:
        svc = self.svc
        p = svc.create_sampling_plan(self.batch["id"], 1, actor=ACTOR_QE)
        item = svc.add_inspection_item(p["id"], "尺寸")
        with self.assertRaises(DomainError):
            svc.record_inspection(item["id"], "缺陷",
                                  evidences=[{"is_defect": False}],
                                  actor=ACTOR_QE)
        # 证据缺严重度
        with self.assertRaises(DomainError):
            svc.record_inspection(item["id"], "缺陷",
                                  evidences=[{"is_defect": True}],
                                  actor=ACTOR_QE)
        # 检验项仍为待检
        fresh = svc.get_inspection_item(item["id"])
        self.assertEqual(fresh["status"], "待检")
        self.assertEqual(fresh["evidences"], [])

    def test_completed_item_is_immutable(self) -> None:
        svc = self.svc
        p = svc.create_sampling_plan(self.batch["id"], 1, actor=ACTOR_QE)
        item = svc.add_inspection_item(p["id"], "尺寸")
        svc.record_inspection(item["id"], "合格", actor=ACTOR_QE)
        with self.assertRaises(DomainError):
            svc.record_inspection(item["id"], "缺陷",
                                  evidences=[{"is_defect": True, "severity": "致命",
                                              "description": "后补"}],
                                  actor=ACTOR_QE)

    def test_judgement_requires_all_items_done(self) -> None:
        svc = self.svc
        p = svc.create_sampling_plan(self.batch["id"], 2, actor=ACTOR_QE)
        i1 = svc.add_inspection_item(p["id"], "尺寸")
        svc.add_inspection_item(p["id"], "外观")
        svc.record_inspection(i1["id"], "合格", actor=ACTOR_QE)
        with self.assertRaises(DomainError):
            svc.judge_sampling(p["id"], "合格", actor=ACTOR_QE)

    def test_judgement_must_match_evidence(self) -> None:
        svc = self.svc
        p = svc.create_sampling_plan(self.batch["id"], 1, actor=ACTOR_QE)
        i1 = svc.add_inspection_item(p["id"], "尺寸")
        svc.record_inspection(i1["id"], "缺陷",
                              evidences=[{"is_defect": True, "severity": "致命",
                                          "description": "裂纹"}],
                              actor=ACTOR_QE)
        with self.assertRaises(DomainError):  # 有缺陷不能判合格
            svc.judge_sampling(p["id"], "合格", actor=ACTOR_QE)
        judged = svc.judge_sampling(p["id"], "不合格", actor=ACTOR_QE)
        self.assertEqual(judged["status"], PLAN_JUDGED)
        # 已判定不可改判
        with self.assertRaises(DomainError):
            svc.judge_sampling(p["id"], "合格", actor=ACTOR_QE)
        # 已判定计划不能再加项
        with self.assertRaises(DomainError):
            svc.add_inspection_item(p["id"], "追加项")
        # 已完成检验项结果不可更改
        with self.assertRaises(DomainError):
            svc.record_inspection(i1["id"], "合格", actor=ACTOR_QE)

    def test_all_pass_cannot_be_judged_reject(self) -> None:
        svc = self.svc
        p = svc.create_sampling_plan(self.batch["id"], 1, actor=ACTOR_QE)
        i1 = svc.add_inspection_item(p["id"], "尺寸")
        svc.record_inspection(i1["id"], "合格", actor=ACTOR_QE)
        with self.assertRaises(DomainError):
            svc.judge_sampling(p["id"], "不合格", actor=ACTOR_QE)
        self.assertEqual(svc.get_sampling_plan(p["id"])["status"], PLAN_INSPECTING)


if __name__ == "__main__":
    unittest.main()
