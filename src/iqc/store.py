"""写服务共用的行级存取助手（必须在事务内调用）。"""
from __future__ import annotations

import sqlite3

from .db import one, rows
from .errors import CapacityError, NotFoundError, StateError


def get_lot(conn: sqlite3.Connection, lot_no: str) -> dict:
    lot = one(conn.execute("SELECT * FROM lots WHERE lot_no = ?", (lot_no,)))
    if lot is None:
        raise NotFoundError(f"到货批次不存在：{lot_no}")
    return lot


def get_part(conn: sqlite3.Connection, part_no: str) -> dict:
    part = one(conn.execute("SELECT * FROM parts WHERE part_no = ?", (part_no,)))
    if part is None:
        raise NotFoundError(f"零部件不存在：{part_no}")
    return part


def get_location(conn: sqlite3.Connection, code: str) -> dict:
    location = one(conn.execute("SELECT * FROM locations WHERE code = ?", (code,)))
    if location is None:
        raise NotFoundError(f"库位不存在：{code}")
    return location


def quarantined_qty(conn: sqlite3.Connection, lot_no: str) -> int:
    row = one(conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS qty FROM stock_units WHERE lot_no = ? AND status = 'QUARANTINED'",
        (lot_no,)))
    return int(row["qty"])


def next_unit_code(conn: sqlite3.Connection, lot_no: str) -> str:
    """为批次生成下一个库存单位编码（拆分时使用）。"""
    seq = 0
    while True:
        seq += 1
        code = f"{lot_no}-U{seq:03d}"
        if one(conn.execute("SELECT 1 AS x FROM stock_units WHERE unit_code = ?", (code,))) is None:
            return code


def change_occupancy(conn: sqlite3.Connection, code: str, delta: int) -> None:
    """条件更新库位占用量：正数校验容量上限，负数校验不为负。"""
    if delta >= 0:
        cur = conn.execute(
            "UPDATE locations SET occupied = occupied + ?"
            " WHERE code = ? AND (capacity IS NULL OR occupied + ? <= capacity)",
            (delta, code, delta))
        if cur.rowcount != 1:
            raise CapacityError(f"库位 {code} 容量不足")
    else:
        cur = conn.execute(
            "UPDATE locations SET occupied = occupied + ? WHERE code = ? AND occupied + ? >= 0",
            (delta, code, delta))
        if cur.rowcount != 1:
            raise StateError(f"库位 {code} 占用量不一致")


def take_units(conn: sqlite3.Connection, *, lot_no: str, qty: int, to_status: str,
               decision_id: int, now: str, to_location: str | None = None) -> list[dict]:
    """从批次隔离库存中划出 qty 并转移到目标状态，必要时拆分库存单位。

    返回每个受影响单位的移动明细；源库位占用量同步减少。
    """
    moved: list[dict] = []
    need = qty
    units = rows(conn.execute(
        "SELECT * FROM stock_units WHERE lot_no = ? AND status = 'QUARANTINED' ORDER BY id",
        (lot_no,)))
    for unit in units:
        if need <= 0:
            break
        take = min(need, unit["qty"])
        if take == unit["qty"]:
            cur = conn.execute(
                "UPDATE stock_units"
                " SET status = ?, location_code = COALESCE(?, location_code), decision_id = ?, updated_at = ?"
                " WHERE id = ? AND status = 'QUARANTINED'",
                (to_status, to_location, decision_id, now, unit["id"]))
            if cur.rowcount != 1:
                raise StateError("库存单位状态已变化，请重试")
            moved.append({"unit_id": unit["id"], "unit_code": unit["unit_code"],
                          "qty": take, "split_from": None})
        else:
            cur = conn.execute(
                "UPDATE stock_units SET qty = qty - ?, updated_at = ?"
                " WHERE id = ? AND status = 'QUARANTINED' AND qty >= ?",
                (take, now, unit["id"], take))
            if cur.rowcount != 1:
                raise StateError("库存单位状态已变化，请重试")
            code = next_unit_code(conn, lot_no)
            new_id = conn.execute(
                "INSERT INTO stock_units"
                " (unit_code, lot_no, part_no, qty, status, location_code, decision_id, parent_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (code, lot_no, unit["part_no"], take, to_status,
                 to_location or unit["location_code"], decision_id, unit["id"], now, now)).lastrowid
            moved.append({"unit_id": new_id, "unit_code": code,
                          "qty": take, "split_from": unit["unit_code"]})
        change_occupancy(conn, unit["location_code"], -take)
        need -= take
    if need > 0:
        raise StateError("在检库存不足")
    return moved
