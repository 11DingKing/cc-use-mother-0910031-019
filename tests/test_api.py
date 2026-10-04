"""HTTP API 端到端测试：真实服务器 + 状态码与错误码映射。"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iqc import App, Database, make_handler


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "api.db")
        cls.app = App(cls.db)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.server.server_address[1]
        cls._thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._thread.join(timeout=5)
        cls._tmp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_full_flow_over_http(self):
        status, _ = self.call("POST", "/parts", {"part_no": "P-1", "name": "齿轮"})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/locations", {"code": "Q-A", "kind": "QUARANTINE",
                                                     "capacity": 1000})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/locations", {"code": "A-A", "kind": "AVAILABLE"})
        self.assertEqual(status, 201)

        status, lot = self.call("POST", "/lots", {
            "lot_no": "L-9001", "part_no": "P-1", "supplier": "供应商乙", "qty": 80,
            "actor": "仓管员", "quarantine_location": "Q-A", "po_ref": "PO-0910031"})
        self.assertEqual(status, 201)
        self.assertEqual(lot["status"], "RECEIVED")

        status, plan = self.call("POST", "/lots/L-9001/sampling-plans", {
            "actor": "质量工程师", "standard": "GB/T 2828.1", "level": "II", "aql": "1.0",
            "sample_size": 20, "accept_qty": 1, "reject_qty": 2,
            "items": [{"name": "外观", "requirement": "无磕碰"},
                      {"name": "硬度", "requirement": "HRC 58-62"}]})
        self.assertEqual(status, 201)
        self.assertEqual(plan["round"], 1)

        item1, item2 = plan["items"]
        status, _ = self.call("POST", f"/lots/L-9001/items/{item1['id']}/result", {
            "result": "PASS", "actor": "质量工程师"})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", f"/lots/L-9001/items/{item2['id']}/result", {
            "result": "PASS", "actor": "质量工程师", "measured": "HRC 60"})
        self.assertEqual(status, 201)

        status, verdict = self.call("POST", "/lots/L-9001/complete-inspection",
                                    {"actor": "质量工程师"})
        self.assertEqual(status, 201)
        self.assertEqual(verdict["result"], "PASS")

        status, released = self.call("POST", "/lots/L-9001/release", {
            "actor": "质量工程师", "reason": "检验合格放行", "target_location": "A-A",
            "request_id": "REL-9001"})
        self.assertEqual(status, 201)
        self.assertEqual(released["decision"]["dtype"], "RELEASE")

        # 幂等重放：相同 request_id 不重复上架
        status, replay = self.call("POST", "/lots/L-9001/release", {
            "actor": "质量工程师", "reason": "检验合格放行", "target_location": "A-A",
            "request_id": "REL-9001"})
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])

        status, issue = self.call("POST", "/issues", {
            "part_no": "P-1", "qty": 30, "actor": "计划员", "request_id": "ISS-9001"})
        self.assertEqual(status, 201)
        self.assertEqual(issue["lines"][0]["lot_no"], "L-9001")

        status, trace = self.call("GET", "/lots/L-9001/trace")
        self.assertEqual(status, 200)
        unit = trace["units"][0]
        self.assertEqual(unit["decision"]["dtype"], "RELEASE")
        self.assertEqual(unit["issues"][0]["qty"], 30)

        status, stock = self.call("GET", "/stock?part_no=P-1")
        self.assertEqual(status, 200)
        self.assertEqual(stock["stock"][0]["AVAILABLE"], 50)
        self.assertEqual(stock["stock"][0]["ISSUED"], 30)

        status, lots = self.call("GET", "/lots?status=RELEASED")
        self.assertEqual(status, 200)
        self.assertIn("L-9001", [lot["lot_no"] for lot in lots["lots"]])

    def test_error_mapping(self):
        status, err = self.call("POST", "/lots/L-404/release", {
            "actor": "质量工程师", "reason": "放行", "target_location": "A-A"})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "not_found")

        self.call("POST", "/parts", {"part_no": "P-2", "name": "垫片"})
        self.call("POST", "/locations", {"code": "Q-B", "kind": "QUARANTINE"})
        self.call("POST", "/lots", {"lot_no": "L-9002", "part_no": "P-2",
                                    "supplier": "供应商丙", "qty": 10, "actor": "仓管员",
                                    "quarantine_location": "Q-B"})
        status, err = self.call("POST", "/lots/L-9002/release", {
            "actor": "质量工程师", "reason": "未检验放行", "target_location": "A-A"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "state_conflict")

        status, err = self.call("POST", "/lots", {"part_no": "P-2"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "validation_error")

        status, err = self.call("POST", "/issues", {"part_no": "P-2", "qty": 5,
                                                    "actor": "计划员"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "insufficient_stock")

    def test_unknown_route_and_health(self):
        status, _ = self.call("GET", "/health")
        self.assertEqual(status, 200)
        status, err = self.call("GET", "/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
