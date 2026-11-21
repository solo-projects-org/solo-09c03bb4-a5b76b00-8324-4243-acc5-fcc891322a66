# 停电事件还原服务

配电调度员停电经过还原：FastAPI 分别接收**告警 / 抢修 / 复电回执**三类输入，SQLite 落库
（每条记录含馈线号、设备号、起止时间、来源流水号），按规则归并告警生成停电事件，
用有效复电回执闭合事件，并以**版本化发布**支持迟到更正的回溯。

在此之上支持**可靠性日档核算**：按计划号登记**计划公告 / 撤销**，按
**成功发布号 + 馈线 + UTC 日期** 核算已闭合事件，把每个事件落在当天的部分切成
**计划内 / 计划外** 时段（重叠覆盖只计一次，逐段列出通知依据），供可靠性月报标出
超出事前公告范围的停电时段。晚录公告若改变分段或依据，对应日档**随入库停供**，
重算成功才恢复供数，旧日档可按号回查。

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

### 计划公告

- **登记**：`POST /api/plan-notices` 按计划号登记馈线、实际公告时刻 `announced_at`、
  计划起止 `planned_start`/`planned_end`；`kind=cancel` 表示撤销该计划（计划起止可空）。
- **幂等**：`(plan_no, announced_at)` 为幂等键。同计划同公告时刻**原样重送只记一次**
  （`created=false`）；**异文拒收**（HTTP 409，不入库）。**同一计划固定馈线**，
  馈线不一致同样 409。
- **核算期间照常补录**：公告登记接口始终可用；晚录公告通过下述停供机制反映到日档。

### 日档核算

- **核算**：`POST /api/daily-archives` 按成功发布号、馈线和 UTC 日期读取**已闭合事件**，
  把每个事件落在当天 `[00:00Z, 次日00:00Z)` 的部分切成**计划内 / 计划外**段：
  - 对每个事件，取**各计划在停电开始时已公告的最后一条通知**（`announced_at <= 事件开始`）；
    **末条为撤销的计划不计**；
  - 各有效计划的计划起止与当天部分求交后合并，**重叠覆盖只计一次**；
  - 逐段记录**通知依据**（`basis`：计划号、公告时刻、计划起止），计划外段依据为空；
  - 汇总 `planned_minutes` / `unplanned_minutes`，月报据此标出超出事前公告范围的时段。
- **依据版本固定**：日档依据**生效时已入库的公告**（`notice_version` = 核算时最大公告 id），
  核算后入库的公告不改变已生成日档的内容。
- **晚录停供**：新公告入库时，若会成为某供数中日档内某事件的末条通知（即改变分段或依据），
  该日档**随入库停供**（`stale`），同一事务生效；`GET .../current` 返回 `stale`，
  月报不得采用。**重算成功才恢复供数**（再次 `POST /api/daily-archives`）。
- **失败保留**：核算的校验、计算、写入在同一事务内，写入失败整体回滚，
  **保留此前供数状态**，并另记一条 `failed` 日档留痕。
- **回查**：旧日档（停供 `stale` / 被取代 `superseded`）均可按日档号
  `GET /api/daily-archives/{id}` 回查，内容不变。

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
| POST | `/api/plan-notices` | 登记计划公告/撤销（幂等；异文 409） |
| GET  | `/api/plan-notices?plan_no=` | 计划公告列表 |
| POST | `/api/daily-archives` | 核算/重算日档（成功发布号+馈线+UTC日期） |
| GET  | `/api/daily-archives?publication_id=&feeder_no=&date=&status=` | 日档列表 |
| GET  | `/api/daily-archives/current?publication_id=&feeder_no=&date=` | 当前日档（serving/stale/none） |
| GET  | `/api/daily-archives/{archive_id}` | 按日档号回查（含停供/被取代旧档） |

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

## Swagger 试跑：补录撤销 → 日档停供 → 重算恢复供数

在 `http://localhost:8000/docs` 依次执行（也可用 `scripts/demo_daily.sh` 一键跑完）：

1. **POST /api/alarms** + **POST /api/restorations** 造一个已闭合事件
   （10:00–11:00 +08:00，即 02:00–03:00 UTC），再 **POST /api/publications** 得到发布号：
   ```json
   {"feeder_no":"F09","device_no":"D01","start_time":"2026-11-21T10:00:00+08:00","end_time":"2026-11-21T10:20:00+08:00","serial_no":"ALM-DAILY-0001"}
   {"feeder_no":"F09","device_no":"D01","restore_time":"2026-11-21T11:00:00+08:00","serial_no":"RST-DAILY-0001"}
   ```
2. **POST /api/plan-notices** 登记计划公告（计划 09:30–10:30 +08:00，前一日 18:00 已公告）：
   ```json
   {"plan_no":"PLAN-D-001","feeder_no":"F09","kind":"notice","announced_at":"2026-11-20T18:00:00+08:00","planned_start":"2026-11-21T09:30:00+08:00","planned_end":"2026-11-21T10:30:00+08:00"}
   ```
3. **POST /api/daily-archives** 核算：
   ```json
   {"publication_id":1,"feeder_no":"F09","date":"2026-11-21"}
   ```
   → 日档 1 `serving`：计划内 30 分钟（02:00–02:30 UTC，`basis` 含 PLAN-D-001 公告）
   + 计划外 30 分钟（02:30–03:00 UTC）。
4. **补录撤销**：**POST /api/plan-notices**（公告时刻 09:00 +08:00，早于停电开始）：
   ```json
   {"plan_no":"PLAN-D-001","feeder_no":"F09","kind":"cancel","announced_at":"2026-11-21T09:00:00+08:00"}
   ```
   → 响应 `staled_archives` 含日档 1；**GET /api/daily-archives/current** 返回 `stale`（停供）。
   撤销**原样重送**返回 `created=false`，只记一次，不重复停供。
5. **重算**：再次 **POST /api/daily-archives**（同参数）→ 日档 2 `serving`：
   末条为撤销，计划不计，整段计划外 60 分钟，`previous_status=stale`；
   **GET /api/daily-archives/current** 恢复 `serving`。
6. **GET /api/daily-archives/1** → 旧日档按号回查：`stale`，原分段与通知依据保留。
7. **异文拒收**：以同 `plan_no` 同 `announced_at` 但不同 `planned_end` 再次提交步骤 2
   → HTTP 409，不入库。

## 目录结构

```
app/
  main.py      FastAPI 路由
  service.py   归并/闭合计算、版本化发布、计划公告登记、日档核算与查询
  db.py        SQLite 连接与建表
  schemas.py   入参模型（Pydantic v2）
tests/         pytest 自测（单元 + API 端到端，含补录与重算交错）
scripts/demo.sh        演示：补录回执拆分 + 回执重送
scripts/demo_daily.sh  演示：补录撤销停供 + 重算恢复供数
Dockerfile / docker-compose.yml
```
