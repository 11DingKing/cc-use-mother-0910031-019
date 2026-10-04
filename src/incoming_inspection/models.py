"""领域常量与错误类型。

状态、决定、角色均与 domain/contract.json 对齐：
- 批次检验状态机
- 隔离库存边界
- 放行上架原子性
- 库存决定追溯
"""
from __future__ import annotations

# ---- 角色（对应 contract.actors）-------------------------------------------
ROLE_PLANNER = "采购计划员"
ROLE_SUPPLIER = "供应商"
ROLE_QE = "质量工程师"
ROLE_KEEPER = "仓储管理员"

ROLES = (ROLE_PLANNER, ROLE_SUPPLIER, ROLE_QE, ROLE_KEEPER)

# ---- 批次检验状态机 ----------------------------------------------------------
BATCH_DRAFT = "草拟"          # 登记后，尚未到货
BATCH_RECEIVED = "待确认"      # 已到货，等待/进行抽检
BATCH_REINSPECTING = "复检中"  # 首检不合格，复检进行中
BATCH_PARTIAL = "部分放行"     # 存在已放行单位，仍有单位待处理
BATCH_ACCEPTED = "全部放行"    # 全部单位均已放行（合格或让步）
BATCH_REJECTED = "全部退货"    # 全部单位均退货
BATCH_CLOSED = "已关闭"        # 退货完成，终态

BATCH_STATES = (
    BATCH_DRAFT,
    BATCH_RECEIVED,
    BATCH_REINSPECTING,
    BATCH_PARTIAL,
    BATCH_ACCEPTED,
    BATCH_REJECTED,
    BATCH_CLOSED,
)

# 批次状态允许的迁移；状态机同时在 SQL 触发器中强制，应用层重复检查以给出
# 可读的错误信息。
BATCH_TRANSITIONS: dict[str, tuple[str, ...]] = {
    BATCH_DRAFT: (BATCH_RECEIVED,),
    BATCH_RECEIVED: (BATCH_PARTIAL, BATCH_REJECTED, BATCH_REINSPECTING),
    BATCH_REINSPECTING: (BATCH_PARTIAL, BATCH_REJECTED),
    BATCH_PARTIAL: (BATCH_ACCEPTED, BATCH_REJECTED),
    BATCH_ACCEPTED: (),
    BATCH_REJECTED: (BATCH_CLOSED,),
    BATCH_CLOSED: (),
}

# ---- 检验状态（抽样计划/检验项）---------------------------------------------
PLAN_PENDING = "待检"
PLAN_INSPECTING = "检验中"
PLAN_JUDGED = "已判定"
PLAN_STATUSES = (PLAN_PENDING, PLAN_INSPECTING, PLAN_JUDGED)

INSPECTION_PENDING = "待检"
INSPECTION_DONE = "已完成"
INSPECTION_STATUSES = (INSPECTION_PENDING, INSPECTION_DONE)

# ---- 库存单位决定 ------------------------------------------------------------
D_PENDING = "待判"
D_ACCEPT = "合格放行"
D_CONCESSION = "让步接收"
D_REJECT = "退货"
D_CHOICES = (D_PENDING, D_ACCEPT, D_CONCESSION, D_REJECT)

# ---- 库存单位状态 ------------------------------------------------------------
STK_QUARANTINE = "隔离"
STK_AVAILABLE = "可用"
STK_RESERVED = "已预留"
STK_ISSUED = "已领料"
STK_RETURNED = "已退货"
STOCK_STATUSES = (
    STK_QUARANTINE,
    STK_AVAILABLE,
    STK_RESERVED,
    STK_ISSUED,
    STK_RETURNED,
)

# ---- 缺陷严重度 --------------------------------------------------------------
SEVERITY_CRITICAL = "致命"
SEVERITY_MAJOR = "严重"
SEVERITY_MINOR = "轻微"
SEVERITIES = (SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR)

# ---- 让步审批状态 ------------------------------------------------------------
CONCESSION_PENDING = "待审批"
CONCESSION_APPROVED = "已批准"
CONCESSION_REJECTED = "已驳回"
CONCESSION_STATUSES = (CONCESSION_PENDING, CONCESSION_APPROVED, CONCESSION_REJECTED)

# ---- 抽样计划判定结果 --------------------------------------------------------
JUDGE_ACCEPT = "合格"
JUDGE_REJECT = "不合格"
JUDGE_CHOICES = (JUDGE_ACCEPT, JUDGE_REJECT)


class DomainError(Exception):
    """业务规则冲突（HTTP 409）。"""

    def __init__(self, message: str, code: str = "domain_conflict") -> None:
        super().__init__(message)
        self.code = code


class NotFoundError(DomainError):
    """资源不存在（HTTP 404）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="not_found")


class ForbiddenError(DomainError):
    """岗位无权执行该操作（HTTP 403）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="forbidden")
