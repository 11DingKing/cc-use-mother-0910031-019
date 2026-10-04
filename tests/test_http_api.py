"""WSGI 接口端到端测试。"""
from __future__ import annotations

import json
import threading
import unittest
from wsgiref.simple_server import make_server
from urllib import error, request

from scenario import unit_ids

from incoming_inspection.api import create_app
from incoming_inspection.services import Service
from incoming_inspection.store import Database

import os
import tempfile


def _free_port_app():
    db_path = os.path.join(tempfile.mkdtemp(prefix="iqi-api-"), "api.sqlite3")
    app = create_app(db_path)
    srv = make_server("127.0.0.1", 0, app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, Service(Database(db_path)), f"http://127.0.0.1:{srv.server_address[1]}"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv, cls.svc, cls.base = _free_port_app()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()

    def call(self, method: str, path: str, payload=None, role=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if role:
            req.add_header("X-Actor-Role", role)
        try:
            with request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health(self) -> None:
        code, body = self.call("GET", "/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_flow_over_http(self) -> None:
        code, q = self.call("POST", "/api/locations",
                            {"code": "QH-" + os.urandom(3).hex(),
                             "zone": "隔离", "capacity": 20}, "keeper")
        self.assertEqual(code, 201)
        _, a = self.call("POST", "/api/locations",
                         {"code": "AH-" + os.urandom(3).hex(),
                          "zone": "合格", "capacity": 20}, "keeper")
        _, b = self.call("POST", "/api/batches",
                         {"batch_no": "BH-" + os.urandom(4).hex(),
                          "part_no": "P-9", "part_name": "垫片",
                          "supplier": "供应商乙", "qty": 2}, "planner")
        code, b = self.call("POST", f"/api/batches/{b['id']}/arrival",
                            {"quarantine_location_id": q["id"]}, "keeper")
        self.assertEqual(code, 200)
        self.assertEqual(b["status"], "待确认")
        uids = unit_ids(b)

        _, plan = self.call("POST", f"/api/batches/{b['id']}/sampling-plans",
                            {"sample_size": 1}, "qe")
        _, item = self.call("POST", f"/api/sampling-plans/{plan['id']}/items",
                            {"name": "厚度"}, "qe")
        code, _ = self.call("POST",
                            f"/api/inspection-items/{item['id']}/result",
                            {"result": "合格"}, "qe")
        self.assertEqual(code, 200)
        _, plan = self.call("POST",
                            f"/api/sampling-plans/{plan['id']}/judgement",
                            {"result": "合格"}, "qe")
        self.assertEqual(plan["result"], "合格")

        code, b = self.call("POST", f"/api/batches/{b['id']}/release",
                            {"unit_ids": uids,
                             "available_location_id": a["id"]}, "keeper")
        self.assertEqual(code, 200, b)
        self.assertEqual(b["status"], "全部放行")

        # 可用库存
        _, stock = self.call("GET", "/api/stock/available")
        self.assertTrue(any(s["id"] == uids[0] for s in stock))

        # 并发预留：连发两个请求，恰有一个成功
        r1 = self.call("POST", "/api/reservations",
                       {"stock_unit_id": uids[0], "work_order": "WO-A"}, "keeper")
        r2 = self.call("POST", "/api/reservations",
                       {"stock_unit_id": uids[0], "work_order": "WO-B"}, "keeper")
        codes = sorted([r1[0], r2[0]])
        self.assertEqual(codes, [201, 409])
        rid = r1[1]["id"] if r1[0] == 201 else r2[1]["id"]
        code, _ = self.call("POST", f"/api/reservations/{rid}/issue", role="keeper")
        self.assertEqual(code, 200)

        # 追溯
        code, tr = self.call("GET", f"/api/stock-units/{uids[0]}/trace")
        self.assertEqual(code, 200)
        self.assertEqual(tr["decisions"][0]["basis_type"], "抽样计划")
        self.assertEqual(len(tr["movements"]), 3)  # 上架 + 预留 + 领料
        self.assertEqual([m["to_status"] for m in tr["movements"]],
                         ["可用", "已预留", "已领料"])

    def test_unknown_role_rejected(self) -> None:
        code, body = self.call("POST", "/api/locations",
                               {"code": "ZZ", "zone": "隔离"}, role="auditor")
        self.assertEqual(code, 409)
        self.assertIn("角色", body["message"])

    def test_segregation_of_duties(self) -> None:
        # 仓储管理员不能登记批次 / 不能判定抽检
        code, body = self.call("POST", "/api/batches",
                               {"batch_no": "SEG-1", "part_no": "P",
                                "part_name": "名", "supplier": "S", "qty": 1},
                               role="keeper")
        self.assertEqual(code, 403)
        # 质量工程师不能建库位
        code, _ = self.call("POST", "/api/locations",
                            {"code": "SEG-Q", "zone": "隔离"}, role="qe")
        self.assertEqual(code, 403)
        # 采购计划员不能判定
        code, _ = self.call("POST", "/api/sampling-plans/x/judgement",
                            {"result": "合格"}, role="planner")
        self.assertEqual(code, 403)
        # 缺少岗位头
        code, body = self.call("POST", "/api/locations",
                               {"code": "SEG-Q2", "zone": "隔离"})
        self.assertEqual(code, 403)

    def test_missing_actor_is_forbidden(self) -> None:
        code, body = self.call("POST", "/api/reservations",
                               {"stock_unit_id": "x", "work_order": "W"})
        self.assertEqual(code, 403)

    def test_not_found(self) -> None:
        code, _ = self.call("GET", "/api/batches/missing")
        self.assertEqual(code, 404)

    def test_unknown_route(self) -> None:
        code, _ = self.call("GET", "/api/nope")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
