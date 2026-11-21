# 停电事件还原服务

配电调度员停电经过还原：FastAPI 分别接收**告警 / 抢修 / 复电回执**三类输入，SQLite 落库
（每条记录含馈线号、设备号、起止时间、来源流水号），按规则归并告警生成停电事件，
用有效复电回执闭合事件，并以**版本化发布**支持迟到更正的回溯。

## 快速开始

```bash
docker compose up --build        # 服务起在 http://localhost:8000
# 打开 Swagger UI： http://localhost:8000/docs
```

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest                 # 运行自测
```

## 规则说明

- **归并**：同一馈线的告警区间相交，或间隔 ≤ 5 分钟（`MERGE_GAP_MINUTES` 可调），归并为同一事件。
- **闭合**：复电回执的复电时刻 ≥ 事件开始时刻即为**有效回执**，事件闭合，结束时间取复电时刻；
  回执之后的告警归入后续事件。
- **未闭合原因**：没有有效回执的事件保持未闭合，`unclosed_reason` 给出原因（如“未收到有效复电回执”）。
- **幂等**：来源流水号 `serial_no` 唯一。重发返回 `created=false`，不重复入库；
  同流水号但内容不一致时返回 `consistent=false` 提示核查。
- **版本化发布**：`POST /api/publications` 每次发布固定三类输入的版本（各自表最大 id），
  重算全部事件并整体落库。迟到补录（回执/告警）导致事件**拆分或合并**后，
  旧发布号仍可通过 `GET /api/events?publication_id=<旧id>` 查询更正前的结果。
- **失败回落**：发布计算失败时记录一条 `failed` 发布（审计用），查询接口自动回落到上一成功发布。
- 时间统一归一化为 UTC 存储与返回（输入必须带时区，如 `2026-11-21T10:00:00+08:00`）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/alarms` | 接收告警 |
| POST | `/api/repairs` | 接收抢修（关联展示，不参与开合） |
| POST | `/api/restorations` | 接收复电回执 |
| POST | `/api/publications` | 发布：固定输入版本并重算事件 |
| GET  | `/api/publications` / `/api/publications/{id}` | 发布列表 / 详情 |
| GET  | `/api/events?publication_id=&feeder_no=` | 查事件（缺省最新成功发布） |
| GET  | `/api/events/{event_id}` | 事件详情（含来源告警、关联抢修） |

事件返回字段：`start_time`/`end_time`（起止）、`devices`（受影响设备）、
`closed`（闭合状态）、`closed_by_serial`（闭合回执流水号）、`unclosed_reason`（未闭合原因）。

## Swagger 试跑：补录复电回执导致拆分 + 回执重送

在 `http://localhost:8000/docs` 依次执行（也可用 `scripts/demo.sh` 一键跑完）：

1. **POST /api/alarms**（两条，间隔正好 5 分钟 → 归并）：
   ```json
   {"feeder_no":"F01","device_no":"D01","start_time":"2026-11-21T10:00:00+08:00","end_time":"2026-11-21T10:20:00+08:00","serial_no":"ALM-20261121-0001"}
   {"feeder_no":"F01","device_no":"D02","start_time":"2026-11-21T10:25:00+08:00","end_time":"2026-11-21T10:45:00+08:00","serial_no":"ALM-20261121-0002"}
   ```
2. **POST /api/publications** → 得到发布 1，`GET /api/events` 可见 **1 个未闭合事件**（10:00–10:45）。
3. **POST /api/restorations** 补录回执（复电时刻落在事件中间）：
   ```json
   {"feeder_no":"F01","device_no":"D01","restore_time":"2026-11-21T10:22:00+08:00","serial_no":"RST-20261121-0001"}
   ```
4. **POST /api/publications** → 发布 2，`GET /api/events` 可见事件**拆分**为两个：
   - 10:00–10:22，`closed=true`，`closed_by_serial=RST-20261121-0001`
   - 10:25–10:45，`closed=false`，`unclosed_reason=未收到有效复电回执`
5. **GET /api/events?publication_id=1** → 旧发布号仍返回更正前的单个未闭合事件。
6. **回执重送**：再次 **POST /api/restorations** 提交同一条 `RST-20261121-0001`
   → 返回 `created=false`（幂等，不重复入库）；再 **POST /api/publications**，
   事件结果与发布 2 完全一致。

## 目录结构

```
app/
  main.py      FastAPI 路由
  service.py   归并/闭合计算、版本化发布、查询
  db.py        SQLite 连接与建表
  schemas.py   入参模型（Pydantic v2）
tests/         pytest 自测（单元 + API 端到端）
scripts/demo.sh  演示脚本（同 Swagger 流程）
Dockerfile / docker-compose.yml
```
