"""零依赖 WSGI HTTP 接口。

路由：
- POST   /api/locations                         建立隔离/合格库位
- GET    /api/locations
- POST   /api/batches                           登记到货批次（草拟）
- GET    /api/batches[?status=...]
- GET    /api/batches/{id}
- POST   /api/batches/{id}/arrival              到货确认（生成隔离单位）
- POST   /api/batches/{id}/reinspection         发起复检
- POST   /api/batches/{id}/release              放行（合格/让步）原子上架
- POST   /api/batches/{id}/reject               判退并出库
- GET    /api/batches/{id}/trace                批次级追溯
- POST   /api/batches/{id}/sampling-plans       建立抽样计划（自动轮次）
- GET    /api/batches/{id}/sampling-plans
- GET    /api/sampling-plans/{id}
- POST   /api/sampling-plans/{id}/start
- POST   /api/sampling-plans/{id}/judgement     合格/不合格判定
- POST   /api/sampling-plans/{id}/items         追加检验项
- POST   /api/inspection-items/{id}/result      录入结果与缺陷证据
- POST   /api/concessions                       让步申请
- POST   /api/concessions/{id}/decision         让步审批
- POST   /api/reservations                      并发领料预留
- POST   /api/reservations/{id}/issue
- POST   /api/reservations/{id}/cancel
- GET    /api/stock-units/{id}/trace            单单位决定依据追溯
- GET    /api/stock/available[?part_no=...]     可用库存
- GET    /healthz

操作人通过 X-Actor-Role 请求头（planner/supplier/qe/keeper）或请求体
actor 字段（中文角色名）传递。
"""
from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import parse_qs

from . import models as _m  # noqa: F401  (保证领域常量已加载)
from .models import DomainError, ForbiddenError, NotFoundError
from .services import Service
from .store import Database

# HTTP 头只能用 latin-1，角色代码映射到契约中的中文角色名
ROLE_CODES = {
    "planner": _m.ROLE_PLANNER,
    "supplier": _m.ROLE_SUPPLIER,
    "qe": _m.ROLE_QE,
    "keeper": _m.ROLE_KEEPER,
}


def _json_response(start_response, status: int, payload: object) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    start_response(f"{status} {_STATUS[status]}", [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


_STATUS = {200: "OK", 201: "Created", 400: "Bad Request", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
           500: "Internal Server Error"}

# 岗位分离：写操作必须由对应岗位发起（读操作不限制）
KEEPER = _m.ROLE_KEEPER
QE = _m.ROLE_QE
PLANNER = _m.ROLE_PLANNER


def _required_roles(method: str, path: str) -> set[str] | None:
    """返回接口允许的岗位集合；None 表示无需岗位（健康检查/只读）。"""
    if method != "POST":
        return None
    if path == "/api/locations":
        return {KEEPER}
    if path == "/api/batches":
        return {PLANNER}
    if path.endswith("/arrival") or path.endswith("/release"):
        return {KEEPER}
    if path.endswith("/reject") or path.endswith("/reinspection"):
        return {QE}
    if path.endswith("/sampling-plans") or path.endswith("/start") \
            or path.endswith("/judgement") or path.endswith("/items") \
            or path.endswith("/result"):
        return {QE}
    if path == "/api/concessions":
        return {PLANNER, QE}
    if path.startswith("/api/concessions/") and path.endswith("/decision"):
        return {QE}
    if path == "/api/reservations" or path.endswith("/issue") \
            or path.endswith("/cancel"):
        return {KEEPER}
    return set()  # 未知写路径：交给路由层报 404，不在这里拦


class Application:
    def __init__(self, service: Service) -> None:
        self.service = service

    # ---- WSGI 入口 --------------------------------------------------------
    def __call__(self, environ, start_response):
        try:
            return self._dispatch(environ, start_response)
        except NotFoundError as exc:
            return _json_response(start_response, 404,
                                  {"error": exc.code, "message": str(exc)})
        except ForbiddenError as exc:
            return _json_response(start_response, 403,
                                  {"error": exc.code, "message": str(exc)})
        except DomainError as exc:
            return _json_response(start_response, 409,
                                  {"error": exc.code, "message": str(exc)})
        except sqlite3.IntegrityError as exc:
            return _json_response(start_response, 409,
                                  {"error": "integrity_violation", "message": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            return _json_response(start_response, 400,
                                  {"error": "bad_request", "message": str(exc)})
        except Exception as exc:  # pragma: no cover - 防御性
            return _json_response(start_response, 500,
                                  {"error": "internal", "message": str(exc)})

    def _dispatch(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/").rstrip("/") or "/"
        if path == "/healthz":
            return _json_response(start_response, 200, {"status": "ok"})
        if not path.startswith("/api/"):
            raise NotFoundError("未知路径")

        qs = parse_qs(environ.get("QUERY_STRING", ""))
        body = self._read_body(environ)
        actor = (self._actor_from_header(environ)
                 or (body.pop("actor", None) if body else None))
        s = self.service

        # 岗位分离检查
        required = _required_roles(method, path)
        if required:
            if not actor:
                raise ForbiddenError("缺少操作岗位（X-Actor-Role 或 actor 字段）")
            if actor not in required:
                raise ForbiddenError(
                    f"岗位 {actor} 无权执行该操作，允许岗位：{'、'.join(sorted(required))}")

        # ---- 库位 ----
        if path == "/api/locations":
            if method == "POST":
                return _json_response(start_response, 201, s.create_location(
                    body["code"], body["zone"], body.get("capacity"), actor))
            if method == "GET":
                return _json_response(start_response, 200,
                                      s.list_locations(qs.get("zone", [None])[0]))

        # ---- 批次 ----
        if path == "/api/batches":
            if method == "POST":
                return _json_response(start_response, 201, s.create_batch(
                    body["batch_no"], body["part_no"], body["part_name"],
                    body["supplier"], int(body["qty"]), body.get("po_no"), actor))
            if method == "GET":
                return _json_response(start_response, 200,
                                      s.list_batches(qs.get("status", [None])[0]))

        m_batch = re.fullmatch(r"/api/batches/([^/]+)", path)
        if m_batch:
            bid = m_batch.group(1)
            if method == "GET":
                return _json_response(start_response, 200, s.get_batch(bid))

        if method == "POST":
            bid = self._batch_subpath(path, "arrival")
            if bid:
                return _json_response(start_response, 200, s.confirm_arrival(
                    bid, body["quarantine_location_id"],
                    body.get("received_qty"), actor))
            bid = self._batch_subpath(path, "reinspection")
            if bid:
                return _json_response(start_response, 200, s.request_reinspection(
                    bid, body.get("reason", ""), actor))
            bid = self._batch_subpath(path, "release")
            if bid:
                return _json_response(start_response, 200, s.release_units(
                    bid, body["unit_ids"], body["available_location_id"],
                    body.get("decision", _m.D_ACCEPT),
                    body.get("concession_ids"), actor))
            bid = self._batch_subpath(path, "reject")
            if bid:
                return _json_response(start_response, 200, s.reject_units(
                    bid, body["unit_ids"], body.get("reason"), actor))

        m_plans = re.fullmatch(r"/api/batches/([^/]+)/sampling-plans", path)
        if m_plans:
            bid = m_plans.group(1)
            if method == "POST":
                return _json_response(start_response, 201, s.create_sampling_plan(
                    bid, int(body["sample_size"]), body.get("plan_type", "正常抽检"),
                    body.get("aql"), actor))
            if method == "GET":
                return _json_response(start_response, 200, s.list_sampling_plans(bid))

        # ---- 抽样计划 / 检验项 ----
        m_plan = re.fullmatch(r"/api/sampling-plans/([^/]+)", path)
        if m_plan and method == "GET":
            return _json_response(start_response, 200,
                                  s.get_sampling_plan(m_plan.group(1)))
        pid = self._subpath(path, "/api/sampling-plans", "start")
        if pid and method == "POST":
            return _json_response(start_response, 200, s.start_sampling(pid, actor))
        pid = self._subpath(path, "/api/sampling-plans", "judgement")
        if pid and method == "POST":
            return _json_response(start_response, 200, s.judge_sampling(
                pid, body["result"], actor))
        pid = self._subpath(path, "/api/sampling-plans", "items")
        if pid and method == "POST":
            return _json_response(start_response, 201, s.add_inspection_item(
                pid, body["name"], body.get("spec_requirement")))

        iid = self._subpath(path, "/api/inspection-items", "result")
        if iid and method == "POST":
            return _json_response(start_response, 200, s.record_inspection(
                iid, body["result"], body.get("measured_value"),
                body.get("evidences", ()), actor))

        # ---- 让步 ----
        if path == "/api/concessions" and method == "POST":
            return _json_response(start_response, 201, s.request_concession(
                body["stock_unit_id"], body["reason"],
                body.get("proposed_use"), actor))
        m_con = re.fullmatch(r"/api/concessions/([^/]+)/decision", path)
        if m_con and method == "POST":
            return _json_response(start_response, 200, s.decide_concession(
                m_con.group(1), bool(body["approve"]),
                body.get("decision_note"), actor))

        # ---- 领料 ----
        if path == "/api/reservations" and method == "POST":
            return _json_response(start_response, 201, s.create_reservation(
                body["stock_unit_id"], body["work_order"], actor))
        rid = self._subpath(path, "/api/reservations", "issue")
        if rid and method == "POST":
            return _json_response(start_response, 200, s.issue_reservation(rid, actor))
        rid = self._subpath(path, "/api/reservations", "cancel")
        if rid and method == "POST":
            return _json_response(start_response, 200,
                                  s.cancel_reservation(rid, actor))

        # ---- 追溯 / 库存 ----
        m_trace = re.fullmatch(r"/api/stock-units/([^/]+)/trace", path)
        if m_trace and method == "GET":
            return _json_response(start_response, 200,
                                  s.trace_unit(m_trace.group(1)))
        m_btrace = re.fullmatch(r"/api/batches/([^/]+)/trace", path)
        if m_btrace and method == "GET":
            return _json_response(start_response, 200,
                                  s.trace_batch(m_btrace.group(1)))
        if path == "/api/stock/available" and method == "GET":
            return _json_response(start_response, 200,
                                  s.available_stock(qs.get("part_no", [None])[0]))

        raise NotFoundError(f"不支持的接口：{method} {path}")

    # ---- 辅助 -------------------------------------------------------------
    @staticmethod
    def _actor_from_header(environ) -> str | None:
        # 优先角色代码（X-Actor-Role: qe / keeper / planner / supplier）
        code = environ.get("HTTP_X_ACTOR_ROLE")
        if code:
            role = ROLE_CODES.get(code.lower())
            if role is None:
                raise DomainError(f"未知角色代码：{code}（可选 planner/supplier/qe/keeper）")
            return role
        # X-Actor 仅接受 latin-1 可编码值；中文操作人请走请求体 actor 字段
        raw = environ.get("HTTP_X_ACTOR")
        if raw:
            try:
                raw.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise DomainError("X-Actor 头仅支持 ASCII，中文操作人请使用"
                                  " X-Actor-Role 或请求体 actor 字段") from exc
        return raw

    @staticmethod
    def _read_body(environ) -> dict:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length == 0:
            return {}
        raw = environ["wsgi.input"].read(length)
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _subpath(path: str, prefix: str, name: str) -> str | None:
        m = re.fullmatch(re.escape(prefix) + r"/([^/]+)/" + re.escape(name), path)
        return m.group(1) if m else None

    @classmethod
    def _batch_subpath(cls, path: str, name: str) -> str | None:
        return cls._subpath(path, "/api/batches", name)


def create_app(db_path: str = "data/incoming.sqlite3") -> Application:
    db = Database(db_path)
    db.initialize()
    return Application(Service(db))
