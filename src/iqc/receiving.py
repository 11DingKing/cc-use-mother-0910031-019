"""到货登记：零部件主数据、库位与到货批次（进入隔离）。"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .db import Database, add_event
from .errors import DuplicateError, ValidationError
from .models import LocationKind, LotStatus, UnitStatus, utc_now_iso
from .store import change_occupancy, get_location, get_part
from .validation import optional_int, optional_text, require_int, require_text


def _normalize_units(lot_no: str, qty: object, units: object) -> tuple[list[dict], int]:
    """解析到货拆分明细；未提供时整个批次生成一个库存单位。"""
    if units is None:
        total = require_int("qty", qty, minimum=1)
        return [{"code": f"{lot_no}-U001", "qty": total}], total
    if not isinstance(units, list) or not units:
        raise ValidationError("units 必须是非空列表")
    specs: list[dict] = []
    for index, raw in enumerate(units, start=1):
        if not isinstance(raw, dict):
            raise ValidationError("units 元素必须是对象")
        unit_qty = require_int("units[].qty", raw.get("qty"), minimum=1)
        code = raw.get("code")
        code = f"{lot_no}-U{index:03d}" if code is None else require_text("units[].code", code)
        specs.append({"code": code, "qty": unit_qty})
    total = sum(spec["qty"] for spec in specs)
    if qty is not None and require_int("qty", qty, minimum=1) != total:
        raise ValidationError(f"qty 与 units 数量之和不一致：{qty} != {total}")
    return specs, total


class ReceivingService:
    """零部件、库位与到货批次登记。"""

    def __init__(self, db: Database, clock: Callable[[], str] = utc_now_iso):
        self._db = db
        self._now = clock

    def create_part(self, *, part_no: str, name: str, spec: str | None = None) -> dict:
        part_no = require_text("part_no", part_no)
        name = require_text("name", name)
        spec = optional_text("spec", spec)
        with self._db.transaction() as conn:
            try:
                conn.execute("INSERT INTO parts (part_no, name, spec) VALUES (?, ?, ?)",
                             (part_no, name, spec))
            except sqlite3.IntegrityError:
                raise DuplicateError(f"零部件已存在：{part_no}") from None
        return {"part_no": part_no, "name": name, "spec": spec}

    def create_location(self, *, code: str, kind: str, capacity: int | None = None) -> dict:
        code = require_text("code", code)
        if kind not in LocationKind.ALL:
            raise ValidationError(f"库位类型必须是 {'/'.join(LocationKind.ALL)}")
        capacity = optional_int("capacity", capacity, minimum=1)
        with self._db.transaction() as conn:
            try:
                conn.execute("INSERT INTO locations (code, kind, capacity) VALUES (?, ?, ?)",
                             (code, kind, capacity))
            except sqlite3.IntegrityError:
                raise DuplicateError(f"库位已存在：{code}") from None
        return {"code": code, "kind": kind, "capacity": capacity, "occupied": 0}

    def receive_lot(self, *, lot_no: str, part_no: str, supplier: str, actor: str,
                    quarantine_location: str, qty: int | None = None,
                    po_ref: str | None = None, units: list[dict] | None = None) -> dict:
        """登记到货批次：批次与全部库存单位原子进入隔离库位。

        任一步失败（容量不足、编码重复等）整体回滚，不会留下半上架状态。
        """
        lot_no = require_text("lot_no", lot_no)
        part_no = require_text("part_no", part_no)
        supplier = require_text("supplier", supplier)
        actor = require_text("actor", actor)
        quarantine_location = require_text("quarantine_location", quarantine_location)
        po_ref = optional_text("po_ref", po_ref)
        specs, total = _normalize_units(lot_no, qty, units)
        now = self._now()
        with self._db.transaction() as conn:
            get_part(conn, part_no)
            location = get_location(conn, quarantine_location)
            if location["kind"] != LocationKind.QUARANTINE:
                raise ValidationError("到货批次必须进入隔离库位")
            change_occupancy(conn, quarantine_location, total)
            try:
                conn.execute(
                    "INSERT INTO lots (lot_no, part_no, supplier, po_ref, qty_received,"
                    " status, inspect_round, received_by, received_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (lot_no, part_no, supplier, po_ref, total, LotStatus.RECEIVED, actor, now, now))
            except sqlite3.IntegrityError:
                raise DuplicateError(f"到货批次已存在：{lot_no}") from None
            for spec in specs:
                try:
                    conn.execute(
                        "INSERT INTO stock_units (unit_code, lot_no, part_no, qty, status,"
                        " location_code, decision_id, parent_id, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                        (spec["code"], lot_no, part_no, spec["qty"],
                         UnitStatus.QUARANTINED, quarantine_location, now, now))
                except sqlite3.IntegrityError:
                    raise DuplicateError(f"库存单位编码重复：{spec['code']}") from None
            add_event(conn, now=now, lot_no=lot_no, entity="lot", entity_id=lot_no,
                      action="LOT_RECEIVED", actor=actor,
                      detail={"part_no": part_no, "supplier": supplier, "qty": total,
                              "location": quarantine_location, "unit_count": len(specs)})
        return {"lot_no": lot_no, "part_no": part_no, "supplier": supplier,
                "qty_received": total, "status": LotStatus.RECEIVED,
                "quarantine_location": quarantine_location, "units": specs}
