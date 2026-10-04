# 到货检验隔离

本项目维护到货检验隔离的领域约定、角色边界与样例，并交付一个零依赖
Python 后端：管理到货批次、抽样计划、检验项、缺陷证据与隔离库位，放行时
**原子更新验收决定与可用库存**，并沿批次追踪每个库存单位的决定依据。

当前契约覆盖采购计划员、供应商、质量工程师、仓储管理员，并明确：

- **批次检验状态机**：草拟 → 待确认 →（复检中）→ 部分放行 → 全部放行 / 全部退货 → 已关闭
- **隔离库存边界**：未放行单位物理上不可能进入合格库位或被生产预留/领料
- **放行上架原子性**：决定、台账、库位移动、批次聚合同一事务，失败整体回滚
- **库存决定追溯**：决定台账与库存流水只追加，每件单位可回溯到抽检判定/让步审批

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/incoming_inspection/`：后端服务
  - `models.py`：角色、状态机、决定、错误类型
  - `store.py`：SQLite 表结构与**触发器硬约束**
  - `services.py`：领域服务（事务脚本）
  - `api.py`：零依赖 WSGI HTTP 接口
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归 + 领域规则 + 并发 + HTTP 端到端测试。

## 关键业务规则

| 场景 | 约束 |
| --- | --- |
| 到货 | 单位只能进入隔离区库位，受容量限制；批次 草拟→待确认 |
| 抽检 | 首检计划只能建在待确认批次；复检轮次要求存在不合格判定且批次处于复检中 |
| 检验项 | 缺陷结果必须附带严重度（致命/严重/轻微）的证据；完成后不可更改 |
| 判定 | 所有检验项完成才能判定，结论须与缺陷证据一致；判定不可改判 |
| 合格放行 | 必须依据**最新一轮**合格判定；决定与上架同事务写入 |
| 退货 | 必须依据最新一轮不合格判定；决定与出库同事务，全退自动关闭批次 |
| 部分放行 | 可只放行/判退部分单位；退放混合批次停留在"部分放行" |
| 复检 | 首检不合格（或部分处置后）可发起，复检结论覆盖处置依据 |
| 让步接收 | 须有**已批准**让步审批单；审批中/已驳回不能放行；审批结论不可改 |
| 并发领料 | 一个单位只能被一张工单预留：`BEGIN IMMEDIATE` + 条件 UPDATE 抢占 |
| 岗位分离 | 登记=采购计划员，检验/判定/退货/复检/让步审批=质量工程师，上架/领料=仓储管理员 |

所有约束都在 SQLite 触发器中二次强制：即使绕过服务层直接执行 SQL，
也无法把待判单位放入合格区、凭未批准让步放行、伪造批次终态或篡改台账。

## 运行

```bash
# 初始化并启动（自动创建数据库与表结构）
PYTHONPATH=src python3 -m incoming_inspection data/incoming.sqlite3 127.0.0.1:8000
```

操作岗位通过请求头 `X-Actor-Role: planner|supplier|qe|keeper`
（或请求体 `actor` 中文名）传递。

### 接口一览

```
POST   /api/locations                           建立隔离/合格库位（keeper）
GET    /api/locations[?zone=隔离|合格]
POST   /api/batches                             登记批次（planner）
GET    /api/batches[?status=...]
GET    /api/batches/{id}
POST   /api/batches/{id}/arrival                到货确认，生成隔离单位（keeper）
POST   /api/batches/{id}/sampling-plans         抽样计划，自动轮次（qe）
GET    /api/batches/{id}/sampling-plans
POST   /api/sampling-plans/{id}/start           开始检验（qe）
POST   /api/sampling-plans/{id}/items           追加检验项（qe）
POST   /api/inspection-items/{id}/result        录入结果+缺陷证据（qe）
POST   /api/sampling-plans/{id}/judgement       合格/不合格判定（qe）
POST   /api/batches/{id}/reinspection           发起复检（qe）
POST   /api/batches/{id}/release                放行原子上架（keeper）
POST   /api/batches/{id}/reject                 判退并出库（qe）
POST   /api/concessions                         让步申请（planner/qe）
POST   /api/concessions/{id}/decision           让步审批（qe）
POST   /api/reservations                        并发领料预留（keeper）
POST   /api/reservations/{id}/issue             发放领料（keeper）
POST   /api/reservations/{id}/cancel            取消预留，单位回可用（keeper）
GET    /api/stock/available[?part_no=...]       可领用库存（仅状态=可用）
GET    /api/stock-units/{id}/trace              单单位决定依据链
GET    /api/batches/{id}/trace                  批次级追溯（各轮检验+每单位依据）
GET    /healthz
```

### 典型时序

```bash
# 1. 基础数据与到货
curl -s localhost:8000/api/locations -H 'X-Actor-Role: keeper' -H 'Content-Type: application/json' \
  -d '{"code":"Q-01","zone":"隔离","capacity":100}'
curl -s localhost:8000/api/batches -H 'X-Actor-Role: planner' -H 'Content-Type: application/json' \
  -d '{"batch_no":"B2026-001","part_no":"M8","part_name":"螺栓","supplier":"华东紧固件","qty":5}'
curl -s localhost:8000/api/batches/$BID/arrival -H 'X-Actor-Role: keeper' \
  -H 'Content-Type: application/json' -d '{"quarantine_location_id":"'$QID'"}'

# 2. 抽检与判定（质量工程师）
curl -s localhost:8000/api/batches/$BID/sampling-plans -H 'X-Actor-Role: qe' \
  -H 'Content-Type: application/json' -d '{"sample_size":3,"aql":"1.0"}'
# 对每个检验项录入；缺陷须带证据：
# /api/inspection-items/{id}/result {"result":"缺陷","evidences":[
#   {"is_defect":true,"severity":"严重","evidence_ref":"IMG-1","description":"锈蚀"}]}
curl -s localhost:8000/api/sampling-plans/$PID/judgement -H 'X-Actor-Role: qe' \
  -H 'Content-Type: application/json' -d '{"result":"不合格"}'

# 3a. 不合格：退货 / 让步 / 复检
curl -s localhost:8000/api/batches/$BID/reject -H 'X-Actor-Role: qe' \
  -H 'Content-Type: application/json' -d '{"unit_ids":[...],"reason":"严重缺陷"}'
# 让步：先申请、质量工程师批准，再带审批单放行
curl -s localhost:8000/api/batches/$BID/release -H 'X-Actor-Role: keeper' \
  -H 'Content-Type: application/json' -d '{"unit_ids":[...],"available_location_id":"'$AID'",
    "decision":"让步接收","concession_ids":{"'$UID'":"'$CID'"}}'
# 复检：reinspection → 新一轮 sampling-plans → 判定 → release

# 3b. 合格：整批或部分放行（原子上架）
curl -s localhost:8000/api/batches/$BID/release -H 'X-Actor-Role: keeper' \
  -H 'Content-Type: application/json' \
  -d '{"unit_ids":[...],"available_location_id":"'$AID'","decision":"合格放行"}'

# 4. 生产并发领料（同一单位只有一个请求成功）
curl -s localhost:8000/api/reservations -H 'X-Actor-Role: keeper' \
  -H 'Content-Type: application/json' -d '{"stock_unit_id":"'$UID'","work_order":"WO-77"}'
curl -s localhost:8000/api/reservations/$RID/issue -X POST -H 'X-Actor-Role: keeper'

# 5. 追溯每件库存的决定依据
curl -s localhost:8000/api/stock-units/$UID/trace
curl -s localhost:8000/api/batches/$BID/trace
```

## 验证

```bash
# 全部测试：契约 + 状态机 + 检验/缺陷 + 让步 + 原子性 + 并发（线程/HTTP）+ 追溯 + 岗位分离
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json
```

## 并发与失败语义

- 每个服务方法使用独立连接，写事务一律 `BEGIN IMMEDIATE`（立即获取保留锁），
  配合 `busy_timeout` 与"条件 UPDATE + rowcount 校验"，并发领料只有一个赢家，
  其余得到 409 业务冲突。
- 放行为多单位单事务：任一件不满足依据/容量/分区条件，已处理的前序单位
  决定与移动全部回滚，不产生只追加台账之外的"半上架"中间态。
- 触发器用 `RAISE(ABORT)` 中止整条语句，事务回滚并返回中文冲突原因，
  服务层统一转译为 409。
