"""HTTP API：基于标准库的 REST 风格接口。

``App.handle`` 与传输层解耦，可直接在测试里调用；
``make_handler`` 把它挂到 ``http.server.ThreadingHTTPServer`` 上。
"""
from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .db import Database
from .disposition import DispositionService
from .errors import DomainError, ValidationError
from .inspection import InspectionService
from .issuing import IssuingService
from .models import utc_now_iso
from .queries import QueryService
from .receiving import ReceivingService


def _to_int(value: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"路径参数 {field} 必须是整数") from None


class App:
    """应用组装与请求分发。"""

    def __init__(self, db: Database, clock=utc_now_iso):
        self.db = db
        self.receiving = ReceivingService(db, clock)
        self.inspection = InspectionService(db, clock)
        self.disposition = DispositionService(db, clock)
        self.issuing = IssuingService(db, clock)
        self.queries = QueryService(db)
        self._routes: list[tuple[str, re.Pattern, object]] = []
        self._register_routes()

    # ------------------------------------------------------------------ 路由
    def _register(self, method: str, pattern: str, handler) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method, regex, handler))

    def _register_routes(self) -> None:
        self._register("GET", "/health", lambda body, query, **_: {"status": "ok"})
        self._register("POST", "/parts", lambda body, query, **_: self.receiving.create_part(
            part_no=body.get("part_no"), name=body.get("name"), spec=body.get("spec")))
        self._register("POST", "/locations", lambda body, query, **_: self.receiving.create_location(
            code=body.get("code"), kind=body.get("kind"), capacity=body.get("capacity")))
        self._register("POST", "/lots", lambda body, query, **_: self.receiving.receive_lot(
            lot_no=body.get("lot_no"), part_no=body.get("part_no"), supplier=body.get("supplier"),
            qty=body.get("qty"), actor=body.get("actor"),
            quarantine_location=body.get("quarantine_location"),
            po_ref=body.get("po_ref"), units=body.get("units")))
        self._register("GET", "/lots", lambda body, query, **_: {"lots": self.queries.list_lots(
            status=query.get("status"), part_no=query.get("part_no"))})
        self._register("GET", "/lots/{lot_no}",
                       lambda body, query, lot_no, **_: self.queries.get_lot(lot_no))
        self._register("GET", "/lots/{lot_no}/units",
                       lambda body, query, lot_no, **_: {"units": self.queries.list_units(lot_no)})
        self._register("GET", "/lots/{lot_no}/trace",
                       lambda body, query, lot_no, **_: self.queries.trace_lot(lot_no))
        self._register("POST", "/lots/{lot_no}/sampling-plans",
                       lambda body, query, lot_no, **_: self.inspection.create_plan(
                           lot_no=lot_no, actor=body.get("actor"), standard=body.get("standard"),
                           level=body.get("level"), aql=body.get("aql"),
                           sample_size=body.get("sample_size"), accept_qty=body.get("accept_qty"),
                           reject_qty=body.get("reject_qty"), items=body.get("items") or []))
        self._register("POST", "/lots/{lot_no}/items/{item_id}/result",
                       lambda body, query, lot_no, item_id, **_: self.inspection.record_item_result(
                           lot_no=lot_no, item_id=_to_int(item_id, "item_id"),
                           result=body.get("result"), defective_qty=body.get("defective_qty", 0),
                           measured=body.get("measured"), actor=body.get("actor"),
                           defects=body.get("defects") or []))
        self._register("POST", "/lots/{lot_no}/defects",
                       lambda body, query, lot_no, **_: self.inspection.add_defect(
                           lot_no=lot_no, defect_code=body.get("defect_code"),
                           severity=body.get("severity"), qty=body.get("qty"),
                           description=body.get("description"), actor=body.get("actor"),
                           item_id=body.get("item_id"), evidence_uri=body.get("evidence_uri")))
        self._register("POST", "/lots/{lot_no}/complete-inspection",
                       lambda body, query, lot_no, **_: self.inspection.complete_inspection(
                           lot_no=lot_no, actor=body.get("actor")))
        self._register("POST", "/lots/{lot_no}/release",
                       lambda body, query, lot_no, **_: self.disposition.release(
                           lot_no=lot_no, actor=body.get("actor"), reason=body.get("reason"),
                           target_location=body.get("target_location"), qty=body.get("qty"),
                           concession_id=body.get("concession_id"),
                           request_id=body.get("request_id")))
        self._register("POST", "/lots/{lot_no}/return",
                       lambda body, query, lot_no, **_: self.disposition.return_to_supplier(
                           lot_no=lot_no, actor=body.get("actor"), reason=body.get("reason"),
                           qty=body.get("qty"), request_id=body.get("request_id")))
        self._register("POST", "/lots/{lot_no}/reinspect",
                       lambda body, query, lot_no, **_: self.disposition.request_reinspect(
                           lot_no=lot_no, actor=body.get("actor"), reason=body.get("reason")))
        self._register("POST", "/lots/{lot_no}/concessions",
                       lambda body, query, lot_no, **_: self.disposition.request_concession(
                           lot_no=lot_no, qty=body.get("qty"), reason=body.get("reason"),
                           actor=body.get("actor")))
        self._register("GET", "/concessions/{concession_id}",
                       lambda body, query, concession_id, **_: self.queries.get_concession(
                           _to_int(concession_id, "concession_id")))
        self._register("POST", "/concessions/{concession_id}/approve",
                       lambda body, query, concession_id, **_: self.disposition.approve_concession(
                           concession_id=_to_int(concession_id, "concession_id"),
                           actor=body.get("actor"), note=body.get("note")))
        self._register("POST", "/concessions/{concession_id}/reject",
                       lambda body, query, concession_id, **_: self.disposition.reject_concession(
                           concession_id=_to_int(concession_id, "concession_id"),
                           actor=body.get("actor"), note=body.get("note")))
        self._register("POST", "/issues", lambda body, query, **_: self.issuing.issue(
            part_no=body.get("part_no"), qty=body.get("qty"), actor=body.get("actor"),
            lot_no=body.get("lot_no"), request_id=body.get("request_id")))
        self._register("GET", "/stock", lambda body, query, **_: {
            "stock": self.queries.stock_summary(part_no=query.get("part_no"))})

    # ------------------------------------------------------------------ 分发
    def handle(self, method: str, path: str, body: dict | None = None,
               query: dict | None = None) -> tuple[int, dict]:
        body = body or {}
        query = query or {}
        for route_method, regex, handler in self._routes:
            if route_method != method:
                continue
            match = regex.match(path)
            if not match:
                continue
            params = {key: unquote(value) for key, value in match.groupdict().items()}
            try:
                result = handler(body, query, **params)
            except DomainError as exc:
                return exc.http_status, {"error": {
                    "code": exc.code, "message": exc.message, "details": exc.details}}
            status = 201 if method == "POST" else 200
            if isinstance(result, dict) and result.get("replayed"):
                status = 200
            return status, result
        return 404, {"error": {"code": "not_found", "message": "接口不存在",
                               "details": {"method": method, "path": path}}}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "IQC/1.0"
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path if len(parsed.path) <= 1 else parsed.path.rstrip("/")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            body: dict = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._send(400, {"error": {"code": "bad_json",
                                                      "message": "请求体不是合法 JSON",
                                                      "details": {}}})
                if not isinstance(body, dict):
                    return self._send(422, {"error": {"code": "validation_error",
                                                      "message": "请求体必须是 JSON 对象",
                                                      "details": {}}})
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                status, payload = app.handle(method, path, body, query)
            except Exception:  # noqa: BLE001 - 兜底，避免连接直接断开
                traceback.print_exc()
                status, payload = 500, {"error": {"code": "internal_error",
                                                  "message": "服务内部错误", "details": {}}}
            self._send(status, payload)

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # 静默访问日志
            pass

    return Handler


def run(host: str = "127.0.0.1", port: int = 8080, db_path: str = "iqc.db") -> None:
    db = Database(db_path)
    app = App(db)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"到货检验隔离服务已启动：http://{host}:{server.server_address[1]}（数据库 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
