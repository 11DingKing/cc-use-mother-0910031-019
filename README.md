# 到货检验隔离

本项目维护到货检验隔离的领域约定、角色边界与样例数据，并提供可运行的 Python 后端：管理到货批次、抽样计划、检验项、缺陷证据与隔离库位，放行时原子更新验收决定与可用库存，支持部分放行、复检、退货、让步审批与并发领料，并可沿批次追溯每个库存单位的决定依据。当前契约覆盖采购计划员、供应商、质量工程师、仓储管理员，并明确批次检验状态机、隔离库存边界、放行上架原子性、库存决定追溯等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例（含批次/库存单位状态机与决定类型）。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/iqc/`：到货检验隔离后端（纯标准库，Python ≥ 3.11）。
  - `receiving.py`：零部件、库位与到货批次登记（批次原子进入隔离库位）。
  - `inspection.py`：抽样计划（Ac/Re）、检验项结果与缺陷证据。
  - `disposition.py`：放行（含部分放行/让步）、退货、复检与让步审批。
  - `issuing.py`：生产领料（FIFO、并发安全、幂等）。
  - `queries.py`：批次详情、库存汇总与全链路追溯。
  - `api.py`：REST 风格 HTTP 接口（`http.server`）。
  - `db.py`：SQLite 表结构与事务边界（`BEGIN IMMEDIATE`）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/serve.py`：命令行启动后端服务。
- `tests/`：契约回归、状态机、原子性、让步/复检/退货、幂等与并发测试。

## 核心规则

- **批次检验状态机**：`RECEIVED → INSPECTING → AWAITING_DISPOSITION → PARTIALLY_RELEASED → RELEASED / RETURNED / CLOSED`；复检决定使批次回到 `INSPECTING` 并开启新一轮抽样（轮次递增）。
- **隔离库存边界**：到货只进隔离库位；领料仅对 `AVAILABLE` 库存单位可见，检验中/隔离中的批次对领料不可见。
- **放行上架原子性**：验收决定、库存转移/拆分、库位占用、批次状态、审计事件在同一事务提交；任一步失败（如目标库位容量不足）整体回滚，不留半上架状态。
- **让步审批**：检验不合格批次必须凭已批准的让步审批放行；审批人不得与申请人相同；放行数量不得超过审批剩余量。
- **库存决定追溯**：`GET /lots/{lot_no}/trace` 返回每个库存单位的当前状态、决定依据（决定类型/原因/决定人/让步审批）与领料去向。
- **幂等重试**：放行、退货、领料支持 `request_id`，相同请求安全重放，不重复上架或扣减。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/parts` | 登记零部件 |
| POST | `/locations` | 登记库位（`QUARANTINE` / `AVAILABLE`，可设容量） |
| POST | `/lots` | 登记到货批次（进入隔离库位，可拆分库存单位） |
| GET | `/lots`、`/lots/{lot_no}`、`/lots/{lot_no}/units` | 批次列表/详情/库存单位 |
| GET | `/lots/{lot_no}/trace` | 沿批次追溯每个库存单位的决定依据 |
| POST | `/lots/{lot_no}/sampling-plans` | 创建抽样计划（含检验项） |
| POST | `/lots/{lot_no}/items/{item_id}/result` | 录入检验项结果（可随附缺陷证据） |
| POST | `/lots/{lot_no}/defects` | 追加缺陷证据 |
| POST | `/lots/{lot_no}/complete-inspection` | 完成检验并按 Ac/Re 判定 |
| POST | `/lots/{lot_no}/release` | 放行（`qty` 缺省为全部；不合格批次需 `concession_id`） |
| POST | `/lots/{lot_no}/return` | 退货（`qty` 缺省为剩余全部） |
| POST | `/lots/{lot_no}/reinspect` | 复检（批次回到检验中） |
| POST | `/lots/{lot_no}/concessions` | 申请让步接收 |
| POST | `/concessions/{id}/approve`、`/concessions/{id}/reject` | 让步审批 |
| POST | `/issues` | 生产领料（可按 `lot_no` 限定批次） |
| GET | `/stock` | 按零部件汇总各状态库存 |

错误响应统一为 `{"error": {"code", "message", "details"}}`，状态码：404 不存在、409 状态/容量/库存冲突、422 参数校验失败。

## 运行

启动服务：`python3 tools/serve.py --db iqc.db --host 127.0.0.1 --port 8080`

示例：

```bash
curl -s -X POST localhost:8080/parts -d '{"part_no":"P-1","name":"齿轮"}'
curl -s -X POST localhost:8080/locations -d '{"code":"Q-1","kind":"QUARANTINE","capacity":1000}'
curl -s -X POST localhost:8080/locations -d '{"code":"A-1","kind":"AVAILABLE"}'
curl -s -X POST localhost:8080/lots -d '{"lot_no":"L-1","part_no":"P-1","supplier":"供应商甲","qty":100,"actor":"仓管员","quarantine_location":"Q-1"}'
curl -s -X POST localhost:8080/lots/L-1/sampling-plans -d '{"actor":"质量工程师","standard":"GB/T 2828.1","level":"II","aql":"1.0","sample_size":20,"accept_qty":1,"reject_qty":2,"items":[{"name":"外观","requirement":"无磕碰"}]}'
curl -s -X POST localhost:8080/lots/L-1/items/1/result -d '{"result":"PASS","actor":"质量工程师"}'
curl -s -X POST localhost:8080/lots/L-1/complete-inspection -d '{"actor":"质量工程师"}'
curl -s -X POST localhost:8080/lots/L-1/release -d '{"actor":"质量工程师","reason":"检验合格放行","target_location":"A-1"}'
curl -s -X POST localhost:8080/issues -d '{"part_no":"P-1","qty":10,"actor":"计划员"}'
curl -s localhost:8080/lots/L-1/trace
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
