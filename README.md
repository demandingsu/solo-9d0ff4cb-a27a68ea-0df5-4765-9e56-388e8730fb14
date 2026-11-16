# 港航 AIS 靠港停留事件 API

岸基 AIS 点位 → 靠港停留事件，供排放核算使用。FastAPI + SQLite（WAL），
提供 Docker Compose 启动、CSV 上传、事件查询与游标变更流。

## 快速开始

```bash
docker compose up --build
```

* Swagger UI: <http://localhost:8000/docs>
* OpenAPI JSON: <http://localhost:8000/openapi.json>
* 健康检查: <http://localhost:8000/healthz>

演示迟到补点与撤回：在 `/docs` 中展开 **demo → POST /demo/run → Try it out →
Execute**。每次调用创建独立的 `DEMO-XXXXXXXX` 港区，不会污染业务数据。

双实例（共享同一 SQLite 卷，验证交错上传不丢更新）：

```bash
docker compose -f docker-compose.dual.yml up --build
# http://localhost:8001/docs 与 http://localhost:8002/docs
```

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 固定港区矩形范围 | `POST /ports`，同一 `code` 重复登记内容一致时幂等（200），不一致 409 |
| 多次上传 CSV | `POST /ports/{code}/uploads`，含船舶号、UTC 时间、经纬度 |
| 同船同刻同坐标去重 | 批次内与库内均去重，计入 `rows_duplicate` |
| 同船同刻异坐标整批拒收 | 返回 409 `conflicting_coordinates`，整批回滚，不留半批结果 |
| 按时间重排 | 每船按 `(event_time, id)` 排序后推导事件 |
| 相邻在港点 ≤ 10 分钟 | 间隔 > 600 秒断开 |
| 首末相距 ≥ 30 分钟 | 不足 1800 秒不形成事件 |
| 出港点断开 | 矩形外的点作为退出标记，关闭当前事件且不并入 |
| 迟到点改变事件 | 未变事件保留 `event_id`；被改变事件旧号 retract + 新号 upsert |
| 游标顺序读取变更 | `GET /ports/{code}/changes?after=<seq>`，严格递增、可续读 |
| 事件点位证据冻结 | 每个 event_id 发布时把其依据的原始点 id 快照写入 `event_evidence`（只增不改） |
| 现役/已撤回事件均可审计 | `GET /ports/{code}/events/{event_id}/evidence`，已撤回编号返回 `status=retracted` 与发布当时的证据 |
| 证据按 point_id keyset 分页 | `after_point_id` + `limit`；快照不可变，分页期间其它实例继续上传也不重不漏 |
| 上传原子生效 | 原始点、现役事件、变更记录、证据快照同一 `BEGIN IMMEDIATE` 事务提交 |
| 两实例交错上传不丢更新 | SQLite WAL + `BEGIN IMMEDIATE` 写串行化 |
| 失败不推进游标 | 事务回滚，`seq` 不前进，无半批结果 |
| 重复上传不重发变更 | `Idempotency-Key`（缺省用内容 SHA-256）唯一约束，重放返回原批次 |

### 事件编号语义

事件以 `(port, vessel, start_time)` 为身份。迟到点只延长结束时间或增加点数时，
仍会 **retract 旧编号 + upsert 新编号**，保证消费方总能拿到完整快照而无需
patch 既有编号；起点不变、载荷不变的事件保留原编号。CSV 中落在港区矩形外的
点位也会入库（作为出港标记），`rows_outside_port` 会统计它们。

### 点位证据（审计）

每个 event_id 在 **upsert 发布的同一事务内**，把构成该事件的原始点 id 冻结到
只增不改的 `event_evidence` 表，供排放核算审计：

* **现役与已撤回事件都能查** `GET /ports/{code}/events/{event_id}/evidence`。
  现役事件取 `events` 表载荷（`status=active`）；已撤回编号从 `event_changes`
  的 upsert 记录还原发布当时的起止时刻与点数（`status=retracted`）。港区下
  不存在的编号（含属于其它港区的编号）返回 404 `event_not_found`。
* **旧号只对应发布时的点位，新号包含补点**。两个编号各自持有独立快照；
  原始点行不可变，因此旧快照是新快照的严格子集，迟到点只出现在新号中。
* **同点重传不能改写旧证据**。同船同刻同坐标的重传在 `raw_points` 去重，
  不产生新点，也不触碰任何 `event_evidence` 行；新编号发布时写入自己的快照。
* **按原始点编号 keyset 分页**（`after_point_id` + `limit`，按 `point_id`
  升序）。快照行在发布后不可变，因此分页期间另一实例继续上传时，续读既不会
  重复也不会遗漏；响应中的 `evidence_count` 恒等于事件的 `point_count`。
* 每个点位返回 `point_id`、船舶号、UTC 时间（epoch 秒与 ISO-8601 两种形式）、
  经纬度以及 **最初上传批次**（`upload_batch_id` 与批次接收时间）。

**升级说明（schema v1 → v2）**：旧库首次启动时自动建表并按当前库存原始点为
**现役**事件重建证据快照（幂等，可用事务保证两实例同时启动也安全）。升级前
已撤回的编号仍可从变更流查到事件头信息，但无法补回逐点证据，其
`evidence_count` 为 0；升级后新发生的撤回不受影响。

### CSV 格式

```csv
vessel,time,lat,lon
SHIP-001,2026-01-01T00:00:00Z,31.20,121.50
SHIP-001,2026-01-01T00:10:00Z,31.201,121.501
```

表头别名（大小写不敏感）：`vessel/船舶号/mmsi/ship_id`、`time/时间/timestamp`、
`lat/纬度`、`lon/lng/longitude/经度`。时间支持 ISO-8601（无时区按 UTC）或
epoch 秒/毫秒。坐标按 1e-6 度取整后比较。上传上限默认 10 MiB / 100,000 行，
可用 `AIS_MAX_UPLOAD_BYTES`、`AIS_MAX_ROWS` 调整。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/ports` | 登记港区矩形范围（幂等） |
| GET | `/ports/{code}` | 读取港区定义 |
| POST | `/ports/{code}/uploads` | 上传 CSV 点位，整批原子生效 |
| GET | `/ports/{code}/events` | 现役事件，按 `event_id` 分页 |
| GET | `/ports/{code}/events/{event_id}/evidence` | 事件发布时的点位证据（现役/已撤回均可），按 `point_id` 分页 |
| GET | `/ports/{code}/changes` | 变更流，`after` 游标 + `limit` |
| POST | `/demo/run` | 迟到补点 / retract 演示（`AIS_DEMO_ENABLED=1`） |
| GET | `/healthz` | 健康检查 |

上传响应示例：

```json
{
  "replayed": false,
  "batch_id": 7,
  "rows_received": 15,
  "rows_new": 15,
  "rows_duplicate": 0,
  "rows_outside_port": 0,
  "event_changes": {"upserted": 3, "retracted": 0},
  "change_cursor": {"from": 12, "to": 14}
}
```

变更响应示例（`GET /ports/CNSHA-PD/changes?after=14`）：

```json
{
  "port_id": 1,
  "next_cursor": 16,
  "has_more": false,
  "changes": [
    {"cursor": 15, "type": "retract", "event_id": 41, "vessel_id": "SHIP-001", "...": "..."},
    {"cursor": 16, "type": "upsert", "event_id": 42, "event": {"event_id": 42, "...": "..."}}
  ]
}
```

`seq` 在全库单调递增，同一港区的游标可能不连续；按 `seq > after` 读取即可。
`GET /events` 返回的 `cursor` 与变更流同源，可用于「先快照、后增量」。

证据响应示例（`GET /ports/CNSHA-PD/events/42/evidence?limit=2`）：

```json
{
  "port_id": 1,
  "event": {
    "event_id": 42,
    "status": "retracted",
    "vessel_id": "SHIP-001",
    "start_time": 1767225600,
    "start_time_utc": "2026-01-01T00:00:00Z",
    "end_time": 1767229200,
    "end_time_utc": "2026-01-01T01:00:00Z",
    "duration_seconds": 3600,
    "point_count": 7,
    "published_batch_id": 3
  },
  "evidence_count": 7,
  "count": 2,
  "has_more": true,
  "next_cursor": 12,
  "points": [
    {
      "point_id": 11,
      "vessel_id": "SHIP-001",
      "event_time": 1767225600,
      "event_time_utc": "2026-01-01T00:00:00Z",
      "lat": 31.2,
      "lon": 121.5,
      "upload_batch_id": 3,
      "upload_batch_received_at": "2026-11-16T08:00:00+00:00"
    }
  ]
}
```

续读时把上一页的 `next_cursor` 作为 `after_point_id` 传入，直到
`has_more=false`；累计点数应等于 `evidence_count`（也等于事件的
`point_count`）。

## 本地开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
AIS_DEMO_ENABLED=1 uvicorn app.main:app --reload
```

测试覆盖：10 分钟/30 分钟边界、出港断开、批内与库内去重、异坐标整批回滚、
迟到点 retract/upsert 与编号保留、两事件合并、幂等重放、游标分页、港区隔离、
双实例（共享 DB 的并发线程与两个 FastAPI 实例）交错上传，以及点位证据：
现役/已撤回编号查询、旧号冻结与新号含补点、同点重传不改写证据、分页不重不漏
（含分页期间另一实例持续上传）、未知编号与跨港区编号 404、v1 旧库启动回填。
`docker build` 未能在本环境执行（无 Docker 守护进程），Compose 文件仅做了
YAML 解析校验。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIS_DB_PATH` | `./data/ais.sqlite3` | SQLite 路径（容器内 `/data/ais.sqlite3`） |
| `AIS_DEMO_ENABLED` | `0`（镜像内为 `1`） | 是否注册 `/demo/run` |
| `AIS_MAX_UPLOAD_BYTES` | `10485760` | 单次上传字节上限 |
| `AIS_MAX_ROWS` | `100000` | 单次上传唯一行数上限 |
| `AIS_SQLITE_BUSY_TIMEOUT_MS` | `30000` | 写锁等待时间，超时返回 503 |

## 已知限制

* 变更流只保留写入的记录、不提供保留期清理；长期运行需自行归档。
* 事件推导按船全量重算该港区点位；单船超大数据量时应改为增量窗口。
* SQLite 共享卷适用于单机多实例；跨主机需换成服务端数据库。
* v1 旧库升级前已撤回的事件编号只能还原事件头信息，无法补回逐点证据
  （升级后新发生的撤回证据完整）。
* 不做鉴权；生产部署应置于反向代理/网关之后。
