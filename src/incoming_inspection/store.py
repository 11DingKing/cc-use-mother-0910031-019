"""SQLite 存储层：表结构与触发器。

关键不变量在数据库层强制，应用层的并发缺陷无法绕过：

1. 批次检验状态机（trg_batch_status_transition / trg_batch_status_consistency）。
2. 隔离库存边界：未作出放行决定的单位不可能进入可用库位/被预留/被领料。
3. 让步接收必须存在已批准的让步审批单。
4. 决定台账/库存流水只追加，且台账决定必须与单位当前决定一致。
5. 库位分区：隔离单位只能在隔离区，可用单位只能在合格区，容量受限。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- ============================ 主数据 ============================
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    zone TEXT NOT NULL CHECK (zone IN ('隔离', '合格')),
    capacity INTEGER CHECK (capacity IS NULL OR capacity > 0),
    created_at TEXT NOT NULL
);

-- ============================ 到货批次 ============================
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    batch_no TEXT NOT NULL UNIQUE,
    po_no TEXT,
    part_no TEXT NOT NULL,
    part_name TEXT NOT NULL,
    supplier TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    received_qty INTEGER CHECK (received_qty IS NULL OR received_qty >= 0),
    status TEXT NOT NULL CHECK (status IN (
        '草拟', '待确认', '复检中', '部分放行', '全部放行', '全部退货', '已关闭'
    )),
    created_by TEXT, created_at TEXT NOT NULL,
    received_at TEXT, updated_at TEXT NOT NULL
);

-- ============================ 库存单位 ============================
CREATE TABLE IF NOT EXISTS stock_units (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    unit_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        '隔离', '可用', '已预留', '已领料', '已退货'
    )),
    decision TEXT NOT NULL DEFAULT '待判' CHECK (decision IN (
        '待判', '合格放行', '让步接收', '退货'
    )),
    location_id TEXT REFERENCES locations(id),
    updated_at TEXT NOT NULL,
    UNIQUE (batch_id, unit_no)
);

-- ============================ 抽样计划与检验项 ============================
CREATE TABLE IF NOT EXISTS sampling_plans (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    round INTEGER NOT NULL CHECK (round >= 1),
    plan_type TEXT NOT NULL DEFAULT '正常抽检',
    sample_size INTEGER NOT NULL CHECK (sample_size > 0),
    aql TEXT,
    status TEXT NOT NULL CHECK (status IN ('待检', '检验中', '已判定')),
    result TEXT CHECK (result IS NULL OR result IN ('合格', '不合格')),
    created_by TEXT, created_at TEXT NOT NULL,
    started_at TEXT,
    judged_by TEXT, judged_at TEXT,
    UNIQUE (batch_id, round)
);

CREATE TABLE IF NOT EXISTS inspection_items (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES sampling_plans(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    spec_requirement TEXT,
    measured_value TEXT,
    status TEXT NOT NULL CHECK (status IN ('待检', '已完成')),
    result TEXT CHECK (result IS NULL OR result IN ('合格', '缺陷')),
    checked_by TEXT, checked_at TEXT,
    UNIQUE (plan_id, seq)
);

CREATE TABLE IF NOT EXISTS defect_evidences (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES inspection_items(id),
    is_defect INTEGER NOT NULL CHECK (is_defect IN (0, 1)),
    severity TEXT CHECK (severity IS NULL OR severity IN ('致命', '严重', '轻微')),
    evidence_ref TEXT,
    description TEXT,
    created_by TEXT, created_at TEXT NOT NULL
);

-- ============================ 让步审批 ============================
CREATE TABLE IF NOT EXISTS concessions (
    id TEXT PRIMARY KEY,
    stock_unit_id TEXT NOT NULL REFERENCES stock_units(id),
    reason TEXT NOT NULL,
    proposed_use TEXT,
    status TEXT NOT NULL CHECK (status IN ('待审批', '已批准', '已驳回')),
    requested_by TEXT, created_at TEXT NOT NULL,
    decided_by TEXT, decided_at TEXT, decision_note TEXT
);
-- 同一单位至多一张有效审批中的申请、至多一张已批准申请
CREATE UNIQUE INDEX IF NOT EXISTS uq_concession_open
    ON concessions(stock_unit_id) WHERE status = '待审批';
CREATE UNIQUE INDEX IF NOT EXISTS uq_concession_approved
    ON concessions(stock_unit_id) WHERE status = '已批准';

-- ============================ 领料预留 ============================
CREATE TABLE IF NOT EXISTS stock_reservations (
    id TEXT PRIMARY KEY,
    reservation_no TEXT NOT NULL UNIQUE,
    stock_unit_id TEXT NOT NULL REFERENCES stock_units(id),
    work_order TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1 CHECK (qty = 1),
    status TEXT NOT NULL CHECK (status IN ('已预留', '已领料', '已取消')),
    created_by TEXT, created_at TEXT NOT NULL,
    issued_at TEXT
);

-- ============================ 台账（只追加） ============================
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    stock_unit_id TEXT NOT NULL REFERENCES stock_units(id),
    from_decision TEXT NOT NULL,
    to_decision TEXT NOT NULL,
    basis_type TEXT NOT NULL CHECK (basis_type IN ('抽样计划', '让步审批', '质量判定')),
    basis_id TEXT NOT NULL,
    reason TEXT,
    actor TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_movements (
    id TEXT PRIMARY KEY,
    stock_unit_id TEXT NOT NULL REFERENCES stock_units(id),
    from_status TEXT, to_status TEXT NOT NULL,
    location_id TEXT REFERENCES locations(id),
    basis_type TEXT, basis_id TEXT,
    actor TEXT, note TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    event TEXT NOT NULL, actor TEXT, detail TEXT,
    created_at TEXT NOT NULL
);

-- ============================ 触发器：批次状态机 ============================
CREATE TRIGGER IF NOT EXISTS trg_batch_status_transition
BEFORE UPDATE OF status ON batches
WHEN NEW.status IS NOT OLD.status
BEGIN
    SELECT CASE
        WHEN OLD.status = '草拟' AND NEW.status NOT IN ('待确认')
            THEN RAISE(ABORT, '非法批次状态迁移：草拟仅可到货确认')
        WHEN OLD.status = '待确认' AND NEW.status NOT IN
                ('复检中', '部分放行', '全部放行', '全部退货')
            THEN RAISE(ABORT, '非法批次状态迁移：待确认')
        WHEN OLD.status = '复检中' AND NEW.status NOT IN
                ('部分放行', '全部放行', '全部退货')
            THEN RAISE(ABORT, '非法批次状态迁移：复检中')
        WHEN OLD.status = '部分放行' AND NEW.status NOT IN
                ('复检中', '全部放行', '全部退货')
            THEN RAISE(ABORT, '非法批次状态迁移：部分放行')
        WHEN OLD.status = '全部放行'
            THEN RAISE(ABORT, '非法批次状态迁移：全部放行已是终态')
        WHEN OLD.status = '全部退货' AND NEW.status NOT IN ('已关闭')
            THEN RAISE(ABORT, '非法批次状态迁移：全部退货仅可关闭')
        WHEN OLD.status = '已关闭'
            THEN RAISE(ABORT, '非法批次状态迁移：已关闭是终态')
    END;
END;

-- 批次新状态必须与单位聚合一致（应用层先更新单位、最后更新批次）
CREATE TRIGGER IF NOT EXISTS trg_batch_status_consistency
AFTER UPDATE OF status ON batches
WHEN NEW.status IS NOT OLD.status
BEGIN
    SELECT CASE
        WHEN NEW.status = '待确认' AND EXISTS (
            SELECT 1 FROM stock_units WHERE batch_id = NEW.id AND decision != '待判'
        ) THEN RAISE(ABORT, '待确认批次不得存在已判定单位')
        WHEN NEW.status = '复检中' AND NOT EXISTS (
            SELECT 1 FROM sampling_plans
            WHERE batch_id = NEW.id AND status = '已判定' AND result = '不合格'
        ) THEN RAISE(ABORT, '复检中要求：存在已判定的不合格抽检计划')
        WHEN NEW.status = '部分放行' AND (
            -- 至少一个单位已有处置（放行或退货）
            NOT EXISTS (SELECT 1 FROM stock_units
                        WHERE batch_id = NEW.id
                        AND decision IN ('合格放行', '让步接收', '退货'))
            -- 不能全部已放行
            OR NOT EXISTS (SELECT 1 FROM stock_units
                           WHERE batch_id = NEW.id
                           AND (decision = '待判' OR decision = '退货'))
            -- 不能全部退货
            OR NOT EXISTS (SELECT 1 FROM stock_units
                           WHERE batch_id = NEW.id
                           AND (decision = '待判'
                                OR decision = '合格放行'
                                OR decision = '让步接收'))
        ) THEN RAISE(ABORT, '部分放行要求：存在已处置单位，且既非全部放行也非全部退货')
        WHEN NEW.status = '全部放行' AND EXISTS (
            SELECT 1 FROM stock_units
            WHERE batch_id = NEW.id AND decision != '合格放行' AND decision != '让步接收'
        ) THEN RAISE(ABORT, '全部放行要求：所有单位均已放行')
        WHEN NEW.status = '全部退货' AND EXISTS (
            SELECT 1 FROM stock_units
            WHERE batch_id = NEW.id AND decision != '退货'
        ) THEN RAISE(ABORT, '全部退货要求：所有单位决定均为退货')
        WHEN NEW.status = '已关闭' AND (
            EXISTS (SELECT 1 FROM stock_units WHERE batch_id = NEW.id AND decision = '待判')
            OR EXISTS (SELECT 1 FROM stock_units
                       WHERE batch_id = NEW.id AND decision = '退货' AND status != '已退货')
        ) THEN RAISE(ABORT, '关闭要求：无待判单位，且退货单位均已出库')
    END;
END;

-- 进入复检中必须有不合格判定计划
CREATE TRIGGER IF NOT EXISTS trg_batch_reinspect_gate
BEFORE UPDATE OF status ON batches
WHEN NEW.status = '复检中' AND OLD.status != '复检中'
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM sampling_plans
            WHERE batch_id = NEW.id AND status = '已判定' AND result = '不合格'
        ) THEN RAISE(ABORT, '不存在不合格抽检计划，不能发起复检')
    END;
END;

-- ============================ 触发器：库存单位 ============================
CREATE TRIGGER IF NOT EXISTS trg_unit_insert
BEFORE INSERT ON stock_units
BEGIN
    SELECT CASE
        WHEN NEW.status != '隔离' OR NEW.decision != '待判'
            THEN RAISE(ABORT, '新入库单位必须处于隔离/待判')
        WHEN NEW.location_id IS NULL
            THEN RAISE(ABORT, '到货单位必须指定隔离库位')
        WHEN (SELECT zone FROM locations WHERE id = NEW.location_id) != '隔离'
            THEN RAISE(ABORT, '到货单位只能进入隔离区库位')
        WHEN (SELECT capacity FROM locations WHERE id = NEW.location_id) IS NOT NULL
             AND (SELECT COUNT(*) FROM stock_units
                  WHERE location_id = NEW.location_id AND status != '已退货')
                 >= (SELECT capacity FROM locations WHERE id = NEW.location_id)
            THEN RAISE(ABORT, '隔离库位容量已满')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_unit_decision
BEFORE UPDATE OF decision ON stock_units
WHEN NEW.decision IS NOT OLD.decision
BEGIN
    SELECT CASE
        WHEN OLD.decision != '待判'
            THEN RAISE(ABORT, '检验决定一旦作出不可更改')
        WHEN NEW.decision NOT IN ('合格放行', '让步接收', '退货')
            THEN RAISE(ABORT, '非法检验决定')
        WHEN NEW.decision = '让步接收' AND NOT EXISTS (
            SELECT 1 FROM concessions
            WHERE stock_unit_id = NEW.id AND status = '已批准'
        ) THEN RAISE(ABORT, '让步接收必须有已批准的让步审批单')
        WHEN NEW.decision = '让步接收' AND NOT EXISTS (
            SELECT 1 FROM sampling_plans
            WHERE batch_id = NEW.batch_id AND status = '已判定'
        ) THEN RAISE(ABORT, '让步接收必须在抽检判定之后进行')
        WHEN NEW.decision IN ('合格放行', '退货') AND NOT EXISTS (
            SELECT 1 FROM sampling_plans
            WHERE batch_id = NEW.batch_id AND status = '已判定'
        ) THEN RAISE(ABORT, '放行/退货必须依据已判定的抽样计划')
        WHEN NEW.decision = '退货' AND (
            SELECT result FROM sampling_plans
            WHERE batch_id = NEW.batch_id AND status = '已判定'
            ORDER BY round DESC LIMIT 1
        ) != '不合格'
            THEN RAISE(ABORT, '最新一轮抽检合格时不得作退货决定')
        WHEN NEW.decision = '合格放行' AND (
            SELECT result FROM sampling_plans
            WHERE batch_id = NEW.batch_id AND status = '已判定'
            ORDER BY round DESC LIMIT 1
        ) != '合格'
            THEN RAISE(ABORT, '最新一轮抽检不合格时不得作合格放行（请退货或走让步接收）')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_unit_status
BEFORE UPDATE OF status ON stock_units
WHEN NEW.status IS NOT OLD.status
BEGIN
    SELECT CASE
        -- 状态迁移矩阵
        WHEN OLD.status = '隔离' AND NEW.status NOT IN ('可用', '已退货')
            THEN RAISE(ABORT, '隔离单位仅可上架可用或退货出库')
        WHEN OLD.status = '可用' AND NEW.status NOT IN ('已预留')
            THEN RAISE(ABORT, '可用单位仅可被预留')
        WHEN OLD.status = '已预留' AND NEW.status NOT IN ('已领料', '可用')
            THEN RAISE(ABORT, '已预留单位仅可领料或取消预留')
        WHEN OLD.status IN ('已领料', '已退货')
            THEN RAISE(ABORT, '已领料/已退货为终态')
        -- 隔离库存边界：决定与物理状态必须一致
        WHEN NEW.status = '可用' AND NEW.decision NOT IN ('合格放行', '让步接收')
            THEN RAISE(ABORT, '未放行单位不得进入可用库存')
        WHEN NEW.status = '已退货' AND NEW.decision != '退货'
            THEN RAISE(ABORT, '仅退货决定的单位可退货出库')
        WHEN NEW.status IN ('已预留', '已领料')
             AND NEW.decision NOT IN ('合格放行', '让步接收')
            THEN RAISE(ABORT, '仅已放行单位可被预留或领料')
        -- 预留/领料必须有凭证
        WHEN NEW.status = '已预留' AND NOT EXISTS (
            SELECT 1 FROM stock_reservations
            WHERE stock_unit_id = NEW.id AND status = '已预留'
        ) THEN RAISE(ABORT, '预留必须存在有效领料预留单')
        WHEN NEW.status = '已领料' AND NOT EXISTS (
            SELECT 1 FROM stock_reservations
            WHERE stock_unit_id = NEW.id AND status = '已领料'
        ) THEN RAISE(ABORT, '领料必须存在已发放的预留单')
        WHEN NEW.status = '可用' AND OLD.status = '已预留' AND EXISTS (
            SELECT 1 FROM stock_reservations
            WHERE stock_unit_id = NEW.id AND status IN ('已预留', '已领料')
        ) THEN RAISE(ABORT, '仍存在有效预留单，单位不能回到可用')
        -- 库位分区
        WHEN NEW.status = '可用' AND (
            NEW.location_id IS NULL
            OR (SELECT zone FROM locations WHERE id = NEW.location_id) != '合格'
        ) THEN RAISE(ABORT, '可用单位必须位于合格区库位')
        WHEN NEW.status = '可用'
             AND (SELECT capacity FROM locations WHERE id = NEW.location_id) IS NOT NULL
             AND (SELECT COUNT(*) FROM stock_units
                  WHERE location_id = NEW.location_id
                    AND status IN ('可用', '已预留', '已领料')
                    AND id != NEW.id)
                 >= (SELECT capacity FROM locations WHERE id = NEW.location_id)
            THEN RAISE(ABORT, '合格库位容量已满')
    END;
END;

-- ============================ 触发器：抽样计划与检验项 ============================
CREATE TRIGGER IF NOT EXISTS trg_plan_insert
BEFORE INSERT ON sampling_plans
BEGIN
    SELECT CASE
        WHEN NEW.round = 1 AND (SELECT status FROM batches WHERE id = NEW.batch_id) != '待确认'
            THEN RAISE(ABORT, '首检计划只能在待确认批次上创建')
        WHEN NEW.round > 1 AND (
            (SELECT status FROM batches WHERE id = NEW.batch_id) != '复检中'
            OR NOT EXISTS (SELECT 1 FROM sampling_plans
                           WHERE batch_id = NEW.batch_id AND result = '不合格')
        ) THEN RAISE(ABORT, '复检计划只能在复检中批次上创建，且需有不合格记录')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_plan_update
BEFORE UPDATE OF status, result ON sampling_plans
BEGIN
    SELECT CASE
        WHEN NEW.status NOT IN ('待检', '检验中', '已判定')
            THEN RAISE(ABORT, '非法计划状态')
        WHEN OLD.status = '待检' AND NEW.status NOT IN ('待检', '检验中')
            THEN RAISE(ABORT, '计划只能从待检进入检验中')
        WHEN OLD.status = '检验中' AND NEW.status NOT IN ('检验中', '已判定')
            THEN RAISE(ABORT, '计划只能从检验中进入已判定')
        WHEN OLD.status = '已判定' AND (
            NEW.status != '已判定' OR IFNULL(NEW.result, '') != IFNULL(OLD.result, '')
        ) THEN RAISE(ABORT, '已判定计划不可更改')
        WHEN NEW.status = '已判定' AND NEW.result IS NULL
            THEN RAISE(ABORT, '判定计划必须给出合格/不合格结论')
        WHEN NEW.status = '已判定' AND NOT EXISTS (
            SELECT 1 FROM inspection_items WHERE plan_id = NEW.id
        ) THEN RAISE(ABORT, '没有检验项的计划不能判定')
        WHEN NEW.status = '已判定' AND EXISTS (
            SELECT 1 FROM inspection_items WHERE plan_id = NEW.id AND status != '已完成'
        ) THEN RAISE(ABORT, '存在未完成检验项，计划不能判定')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_item_insert
BEFORE INSERT ON inspection_items
BEGIN
    SELECT CASE
        WHEN (SELECT status FROM sampling_plans WHERE id = NEW.plan_id) = '已判定'
            THEN RAISE(ABORT, '不能向已判定计划追加检验项')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_item_update
BEFORE UPDATE OF status, result, measured_value ON inspection_items
BEGIN
    SELECT CASE
        WHEN OLD.status = '已完成' AND NEW.status = '已完成'
             AND IFNULL(NEW.result, '') != IFNULL(OLD.result, '')
            THEN RAISE(ABORT, '已完成检验项结果不可更改')
        WHEN OLD.status = '已完成' AND NEW.status != '已完成'
            THEN RAISE(ABORT, '已完成检验项不可回退')
        WHEN NEW.status = '已完成' AND NEW.result IS NULL
            THEN RAISE(ABORT, '完成检验项必须记录合格/缺陷结果')
        WHEN (SELECT status FROM sampling_plans WHERE id = NEW.plan_id) = '已判定'
            THEN RAISE(ABORT, '计划已判定，不能再录入检验项')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_insert
BEFORE INSERT ON defect_evidences
BEGIN
    SELECT CASE
        WHEN NEW.is_defect = 1 AND NEW.severity IS NULL
            THEN RAISE(ABORT, '缺陷证据必须标注严重度')
        WHEN NEW.is_defect = 0 AND NEW.severity IS NOT NULL
            THEN RAISE(ABORT, '非缺陷证据不得标注严重度')
    END;
END;

-- ============================ 触发器：让步审批 ============================
CREATE TRIGGER IF NOT EXISTS trg_concession_insert
BEFORE INSERT ON concessions
BEGIN
    SELECT CASE
        WHEN NEW.status != '待审批'
            THEN RAISE(ABORT, '新建让步申请必须为待审批')
        WHEN (SELECT decision FROM stock_units WHERE id = NEW.stock_unit_id) != '待判'
            THEN RAISE(ABORT, '只能对未判定单位申请让步')
        WHEN (SELECT status FROM batches WHERE id =
                (SELECT batch_id FROM stock_units WHERE id = NEW.stock_unit_id))
             IN ('全部退货', '已关闭')
            THEN RAISE(ABORT, '批次已退货或关闭，不能申请让步')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_concession_update
BEFORE UPDATE OF status ON concessions
WHEN NEW.status IS NOT OLD.status
BEGIN
    SELECT CASE
        WHEN OLD.status != '待审批'
            THEN RAISE(ABORT, '让步审批结论一经作出不可更改')
        WHEN NEW.status NOT IN ('已批准', '已驳回')
            THEN RAISE(ABORT, '非法让步审批结论')
    END;
END;

-- ============================ 触发器：领料预留 ============================
CREATE TRIGGER IF NOT EXISTS trg_reservation_insert
BEFORE INSERT ON stock_reservations
BEGIN
    SELECT CASE
        WHEN NEW.status != '已预留'
            THEN RAISE(ABORT, '新建预留单必须为已预留')
        WHEN (SELECT status FROM stock_units WHERE id = NEW.stock_unit_id) != '可用'
            THEN RAISE(ABORT, '只能预留可用状态的库存单位')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_reservation_update
BEFORE UPDATE OF status ON stock_reservations
WHEN NEW.status IS NOT OLD.status
BEGIN
    SELECT CASE
        WHEN OLD.status = '已预留' AND NEW.status NOT IN ('已领料', '已取消')
            THEN RAISE(ABORT, '预留单只能发放或取消')
        WHEN OLD.status != '已预留'
            THEN RAISE(ABORT, '已终结的预留单不可更改')
        WHEN NEW.status = '已领料'
             AND (SELECT status FROM stock_units WHERE id = NEW.stock_unit_id) NOT IN
                 ('已预留', '已领料')
            THEN RAISE(ABORT, '单位不在预留状态，不能发放领料')
        WHEN NEW.status = '已取消'
             AND (SELECT status FROM stock_units WHERE id = NEW.stock_unit_id) = '已领料'
            THEN RAISE(ABORT, '已领料的预留单不能取消')
    END;
END;

-- ============================ 触发器：只追加台账 ============================
CREATE TRIGGER IF NOT EXISTS trg_decisions_immutable_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, '决定台账只追加，不可修改');
END;
CREATE TRIGGER IF NOT EXISTS trg_decisions_immutable_delete
BEFORE DELETE ON decisions
BEGIN
    SELECT RAISE(ABORT, '决定台账只追加，不可删除');
END;
CREATE TRIGGER IF NOT EXISTS trg_decision_insert_consistent
BEFORE INSERT ON decisions
BEGIN
    SELECT CASE
        WHEN (SELECT decision FROM stock_units WHERE id = NEW.stock_unit_id)
             != NEW.to_decision
            THEN RAISE(ABORT, '决定台账必须与单位当前决定一致')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_movements_immutable_update
BEFORE UPDATE ON inventory_movements
BEGIN
    SELECT RAISE(ABORT, '库存流水只追加，不可修改');
END;
CREATE TRIGGER IF NOT EXISTS trg_movements_immutable_delete
BEFORE DELETE ON inventory_movements
BEGIN
    SELECT RAISE(ABORT, '库存流水只追加，不可删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_immutable_update
BEFORE UPDATE ON event_log
BEGIN
    SELECT RAISE(ABORT, '事件日志只追加，不可修改');
END;
CREATE TRIGGER IF NOT EXISTS trg_events_immutable_delete
BEFORE DELETE ON event_log
BEGIN
    SELECT RAISE(ABORT, '事件日志只追加，不可删除');
END;
"""


class Database:
    """SQLite 连接工厂与结构初始化。"""

    def __init__(self, path: str | Path = "data/incoming.sqlite3") -> None:
        self.path = str(path)

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        # 每个连接必须单独开启外键与忙碌等待
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn
