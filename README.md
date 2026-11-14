# 田间湿度质控平台（Moisture QC Platform）

传感器湿度读数的**上传 / 勘误 / 作废 / 迟到处理**，以及训练前的**湿度突跳复核 API**。
质控员在模型训练前核查湿度突跳，避免把传感器异常误当成真实墒情变化。

- 在线接口文档（Swagger UI）：`http://localhost:8001/docs`（实例 2 为 `:8002/docs`）
- OpenAPI Schema：`/openapi.json`，健康检查：`/health`

---

## 1. 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

启动后包含 **3 个容器**：

| 服务   | 地址            | 说明                                   |
| ------ | --------------- | -------------------------------------- |
| db     | localhost:5432  | PostgreSQL 16，两个应用实例共用的数据库 |
| api-1  | localhost:8001  | 应用实例 1                              |
| api-2  | localhost:8002  | 应用实例 2（与 api-1 共用同一数据库）   |

验证：

```bash
curl http://localhost:8001/health
curl http://localhost:8002/health
open http://localhost:8001/docs        # 或浏览器直接访问 /docs
```

运行端到端演示（标准库实现，无需额外依赖）：

```bash
python3 samples/demo.py http://localhost:8001
```

验证“两个实例共用数据库”的并发竞争（应观察到每次竞争恰好一个 200、一个 409）：

```bash
python3 scripts/two_instance_race.py http://localhost:8001 http://localhost:8002
```

---

## 2. 无 Docker / 本地开发（SQLite 零依赖模式）

不设置 `DATABASE_URL` 或指向 SQLite 即可开箱运行，便于本地开发与 CI：

```bash
export DATABASE_URL="sqlite+pysqlite:////tmp/moisture.db"
uvicorn app.main:app --port 8001
python3 samples/demo.py http://localhost:8001
```

运行测试：

```bash
pytest
```

---

## 3. 核心业务规则

### 3.1 复核查询（新增 API）

`GET /api/v1/fields/{field_id}/review?threshold=10`

- 只取**当前有效读数**（`is_active=true` 且未作废）；旧修订号、作废行不参与。
- 按**采样时刻**排序，仅对采样时刻**相邻**的两条读数判断差值。
- `|湿度差| > threshold` 才返回读数对（差值等于阈值不算突跳），返回
  **读数号、修订号、差值及结论状态**。
- **同一采样时刻存在多条有效读数 → 标为 `pending_check`（待核），不任意配对**。
- 响应携带 `snapshot_version`：本次读数对与结论来自的同一快照版本号。

### 3.2 质控员确认 / 驳回

`POST /api/v1/fields/{field_id}/review/decisions`

质控员依据**所见版本**（请求体 `base_version = snapshot_version`）确认
（`confirmed`）或驳回（`rejected`）。

### 3.3 勘误 / 作废 / 迟到读数

- `correction`（勘误）：旧行置为非活动并保留，插入 `revision+1` 的新行，
  通过 `supersedes_id` 关联。
- `void`（作废）：标记 `is_active=false, is_void=true`，不删除。
- `late`（迟到）：正常插入并标记 `is_late=true`，可能插入到历史采样时刻
  之间从而**改变相邻关系**。

数值或相邻关系变化后，受影响的旧结论 **`is_effective=false`（不再显示为有效），
但行保留供追溯**；`GET .../review/conclusions?include_inactive=true` 可查全部历史结论。

---

## 4. 并发与一致性保证（题目重点）

两个应用实例共用同一个 PostgreSQL 数据库。

| 风险场景                       | 机制                                                                 | 结果                |
| ------------------------------ | -------------------------------------------------------------------- | ------------------- |
| 上传与复核并发 / 两人同时复核  | 每田块事务级**咨询锁** `pg_advisory_xact_lock`（SQLite 用 `BEGIN IMMEDIATE` 等价串行化）+ 每田块单调版本号 `field_versions.version` | 写事务按田块串行     |
| 基于过期快照的提交             | 请求携带 `expected_version`（上传）/ `base_version`（复核），不匹配当前版本 | **HTTP 409 拒绝，不改写原结论** |
| 唯一索引层面的竞争             | 会话层把唯一约束冲突统一翻译为 409 过期冲突                          | 绝不返回 500，不产生重复有效结论 |
| 查询中读数对与结论来自不同状态 | 复核查询在 **REPEATABLE READ** 快照事务内完成（SQLite 为单快照事务） | 读数对与结论必来自同一快照 |
| 结论被重复下结论               | 结论表**仅追加**：新结论使旧结论 `is_effective=false` 后再插入        | 历史可追溯，不覆盖   |

部分唯一索引保证：每个读数号至多一条有效行、每个读数对身份至多一条有效结论，
同时不阻碍历史行累积。

---

## 5. API 一览（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/fields` | 创建田块 |
| GET  | `/fields` | 田块列表 |
| POST | `/fields/{id}/readings/batch` | 上传 / 勘误 / 作废 / 迟到（支持 `expected_version`） |
| GET  | `/fields/{id}/readings` | 读数全量历史（含旧修订、作废行） |
| **GET**  | **`/fields/{id}/review?threshold=`** | **突跳复核查询（同一快照，返回读数号/修订号/差值/结论）** |
| **POST** | **`/fields/{id}/review/decisions`** | **确认 / 驳回（`base_version` 乐观锁）** |
| GET  | `/fields/{id}/review/conclusions` | 结论追溯（含已失效结论） |
| GET  | `/events` | 历史回放（事件流，支持 `after_seq` 游标） |

完整请求体见 [`samples/`](./samples) 下的 JSON 与 `requests.http`，
真实运行输出见 [`samples/sample_output.txt`](./samples/sample_output.txt)。

---

## 6. 目录结构

```
app/
  config.py            配置（DATABASE_URL，PostgreSQL/SQLite 双方言）
  db.py                引擎、会话、快照事务、咨询锁、并发冲突翻译
  models.py            ORM：读数(版本化)、结论(仅追加)、版本号、事件流
  schemas.py           Pydantic 请求/响应模型
  errors.py            领域异常（404/409）
  services/
    detection.py       突跳检测算法（相邻配对 + 同时刻待核）
    upload.py          上传/勘误/作废/迟到 + 结论失效
    review.py          快照复核查询 + 确认/驳回
    history.py         田块管理与事件回放
  routers.py / main.py 路由与应用入口
samples/               接口样例、demo 脚本与样例输出
scripts/               双实例并发竞争校验脚本
tests/                 18 个端到端与并发测试
docker-compose.yml     PostgreSQL + 两个应用实例
Dockerfile             应用镜像
```

---

## 7. 测试覆盖

```
tests/test_api.py        上传、突跳检测、待核、确认/驳回、勘误失效、
                         作废、迟到改变相邻、过期 409、历史回放、参数校验
tests/test_concurrency.py 并发上传/并发复核竞争、上传与复核竞争、
                         快照一致性（模拟两实例共用数据库）
```
