#!/usr/bin/env bash
# 日档演示：与 Swagger 试跑相同的流程（核算 -> 补录撤销停供 -> 重算恢复供数 -> 旧档回查）。
# 用法：先启动服务（docker compose up --build 或 uvicorn app.main:app），再执行 ./scripts/demo_daily.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
jq_id() { python3 -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }

step "1. 造一个已闭合事件：告警 10:00-10:20(+08:00)，回执 11:00(+08:00)"
curl -sS -X POST "$BASE/api/alarms" -H 'Content-Type: application/json' -d '{
  "feeder_no":"F09","device_no":"D01",
  "start_time":"2026-11-21T10:00:00+08:00","end_time":"2026-11-21T10:20:00+08:00",
  "serial_no":"ALM-DAILY-0001"}'; echo
curl -sS -X POST "$BASE/api/restorations" -H 'Content-Type: application/json' -d '{
  "feeder_no":"F09","device_no":"D01",
  "restore_time":"2026-11-21T11:00:00+08:00",
  "serial_no":"RST-DAILY-0001"}'; echo
PUB=$(curl -sS -X POST "$BASE/api/publications" | jq_id publication_id)
echo "publication_id=$PUB（事件 10:00-11:00+08:00 = 02:00-03:00 UTC，已闭合）"

step "2. 登记计划公告：PLAN-D-001 计划 09:30-10:30(+08:00)，20 日 18:00 已公告"
curl -sS -X POST "$BASE/api/plan-notices" -H 'Content-Type: application/json' -d '{
  "plan_no":"PLAN-D-001","feeder_no":"F09","kind":"notice",
  "announced_at":"2026-11-20T18:00:00+08:00",
  "planned_start":"2026-11-21T09:30:00+08:00","planned_end":"2026-11-21T10:30:00+08:00"}'; echo

step "3. 核算日档（发布 $PUB / F09 / 2026-11-21）：计划内 30 分钟 + 计划外 30 分钟"
A1=$(curl -sS -X POST "$BASE/api/daily-archives" -H 'Content-Type: application/json' -d "{
  \"publication_id\":$PUB,\"feeder_no\":\"F09\",\"date\":\"2026-11-21\"}")
echo "$A1" | python3 -m json.tool
A1_ID=$(echo "$A1" | jq_id archive_id)

step "4. 补录撤销（公告时刻 09:00+08:00，早于停电开始）-> 日档 $A1_ID 随入库停供"
curl -sS -X POST "$BASE/api/plan-notices" -H 'Content-Type: application/json' -d '{
  "plan_no":"PLAN-D-001","feeder_no":"F09","kind":"cancel",
  "announced_at":"2026-11-21T09:00:00+08:00"}'; echo
echo "-- 当前日档状态（stale=停供待重算）"
curl -sS "$BASE/api/daily-archives/current?publication_id=$PUB&feeder_no=F09&date=2026-11-21" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], "archive_id=", d["archive"]["archive_id"])'

step "5. 撤销原样重送：created=false，幂等只记一次，不重复停供"
curl -sS -X POST "$BASE/api/plan-notices" -H 'Content-Type: application/json' -d '{
  "plan_no":"PLAN-D-001","feeder_no":"F09","kind":"cancel",
  "announced_at":"2026-11-21T09:00:00+08:00"}'; echo

step "6. 重算：末条为撤销计划不计，整段计划外 60 分钟，恢复供数"
A2=$(curl -sS -X POST "$BASE/api/daily-archives" -H 'Content-Type: application/json' -d "{
  \"publication_id\":$PUB,\"feeder_no\":\"F09\",\"date\":\"2026-11-21\"}")
echo "$A2" | python3 -m json.tool
A2_ID=$(echo "$A2" | jq_id archive_id)
echo "-- 当前日档状态（serving=重新供数）"
curl -sS "$BASE/api/daily-archives/current?publication_id=$PUB&feeder_no=F09&date=2026-11-21" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], "archive_id=", d["archive"]["archive_id"])'

step "7. 旧日档按号回查：$A1_ID（停供，保留原分段）与 $A2_ID（供数中）"
curl -sS "$BASE/api/daily-archives/$A1_ID" | python3 -m json.tool
curl -sS "$BASE/api/daily-archives/$A2_ID" | python3 -m json.tool

step "8. 异文拒收：同计划同公告时刻但内容不同 -> 409"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/api/plan-notices" \
  -H 'Content-Type: application/json' -d '{
  "plan_no":"PLAN-D-001","feeder_no":"F09","kind":"notice",
  "announced_at":"2026-11-20T18:00:00+08:00",
  "planned_start":"2026-11-21T09:30:00+08:00","planned_end":"2026-11-21T11:00:00+08:00"}'
