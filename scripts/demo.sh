#!/usr/bin/env bash
# 演示脚本：与 Swagger 试跑相同的流程（补录复电回执导致拆分 + 回执重送幂等）。
# 用法：先启动服务（docker compose up --build 或 uvicorn app.main:app），再执行 ./scripts/demo.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 接收两条间隔 5 分钟的告警（F01，D01/D02）"
curl -sS -X POST "$BASE/api/alarms" -H 'Content-Type: application/json' -d '{
  "feeder_no":"F01","device_no":"D01",
  "start_time":"2026-11-21T10:00:00+08:00","end_time":"2026-11-21T10:20:00+08:00",
  "serial_no":"ALM-20261121-0001"}'; echo
curl -sS -X POST "$BASE/api/alarms" -H 'Content-Type: application/json' -d '{
  "feeder_no":"F01","device_no":"D02",
  "start_time":"2026-11-21T10:25:00+08:00","end_time":"2026-11-21T10:45:00+08:00",
  "serial_no":"ALM-20261121-0002"}'; echo

step "2. 第一次发布：归并为一个未闭合事件"
P1=$(curl -sS -X POST "$BASE/api/publications" | python3 -c 'import json,sys; print(json.load(sys.stdin)["publication_id"])')
echo "publication_id=$P1"
curl -sS "$BASE/api/events?publication_id=$P1" | python3 -m json.tool

step "3. 补录复电回执（复电时刻 10:22，落在事件中间）"
curl -sS -X POST "$BASE/api/restorations" -H 'Content-Type: application/json' -d '{
  "feeder_no":"F01","device_no":"D01",
  "restore_time":"2026-11-21T10:22:00+08:00",
  "serial_no":"RST-20261121-0001"}'; echo

step "4. 第二次发布：事件拆分为 已闭合(10:00-10:22) + 未闭合(10:25-10:45)"
P2=$(curl -sS -X POST "$BASE/api/publications" | python3 -c 'import json,sys; print(json.load(sys.stdin)["publication_id"])')
echo "publication_id=$P2"
curl -sS "$BASE/api/events" | python3 -m json.tool

step "5. 旧发布号 $P1 仍可查更正前（拆分前）的结果"
curl -sS "$BASE/api/events?publication_id=$P1" | python3 -m json.tool

step "6. 回执重送（同流水号 RST-20261121-0001）：created=false，幂等"
curl -sS -X POST "$BASE/api/restorations" -H 'Content-Type: application/json' -d '{
  "feeder_no":"F01","device_no":"D01",
  "restore_time":"2026-11-21T10:22:00+08:00",
  "serial_no":"RST-20261121-0001"}'; echo

step "7. 第三次发布：结果与第二次一致（重送无副作用）"
curl -sS -X POST "$BASE/api/publications" | python3 -m json.tool
