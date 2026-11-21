"""API 端到端测试：走一遍 Swagger 演示流程（补录回执拆分 + 回执重送）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTAGE_DB_PATH", str(tmp_path / "api.db"))
    init_db()
    with TestClient(app) as c:
        yield c


ALARM_1 = {"feeder_no": "F01", "device_no": "D01",
           "start_time": "2026-11-21T10:00:00+08:00", "end_time": "2026-11-21T10:20:00+08:00",
           "serial_no": "ALM-20261121-0001"}
ALARM_2 = {"feeder_no": "F01", "device_no": "D02",
           "start_time": "2026-11-21T10:25:00+08:00", "end_time": "2026-11-21T10:45:00+08:00",
           "serial_no": "ALM-20261121-0002"}
RECEIPT = {"feeder_no": "F01", "device_no": "D01",
           "restore_time": "2026-11-21T10:22:00+08:00", "serial_no": "RST-20261121-0001"}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_swagger_demo_flow(client):
    # 1) 两条间隔 5 分钟的告警 -> 归并为一个事件
    assert client.post("/api/alarms", json=ALARM_1).json()["created"] is True
    assert client.post("/api/alarms", json=ALARM_2).json()["created"] is True
    p1 = client.post("/api/publications").json()
    assert p1["status"] == "success" and p1["event_count"] == 1

    # 2) 补录复电回执（复电时刻落在事件中间）-> 发布后出现拆分
    assert client.post("/api/restorations", json=RECEIPT).json()["created"] is True
    p2 = client.post("/api/publications").json()
    assert p2["event_count"] == 2

    events = client.get("/api/events").json()["events"]
    assert events[0]["closed"] is True
    assert events[0]["closed_by_serial"] == "RST-20261121-0001"
    assert events[0]["end_time"] == "2026-11-21T02:22:00.000000+00:00"  # UTC 归一化
    assert events[1]["closed"] is False
    assert events[1]["unclosed_reason"] == "未收到有效复电回执"

    # 3) 旧发布号仍可查更正前的一个事件
    old = client.get(f"/api/events?publication_id={p1['publication_id']}").json()["events"]
    assert len(old) == 1 and old[0]["closed"] is False

    # 4) 回执重送：幂等，不重复入库；再发布结果不变
    resend = client.post("/api/restorations", json=RECEIPT).json()
    assert resend["created"] is False and resend["consistent"] is True
    p3 = client.post("/api/publications").json()
    assert p3["event_count"] == 2
    assert p3["versions"] == p2["versions"]


def test_naive_datetime_rejected(client):
    bad = dict(ALARM_1, start_time="2026-11-21T10:00:00", serial_no="ALM-BAD")
    assert client.post("/api/alarms", json=bad).status_code == 422


def test_end_before_start_rejected(client):
    bad = dict(ALARM_1, end_time="2026-11-21T09:00:00+08:00", serial_no="ALM-BAD2")
    assert client.post("/api/alarms", json=bad).status_code == 422


def test_events_before_any_publish_404(client):
    assert client.get("/api/events").status_code == 404


def test_event_detail_includes_sources(client):
    client.post("/api/alarms", json=ALARM_1)
    client.post("/api/repairs", json={"feeder_no": "F01", "device_no": "D01",
                                      "start_time": "2026-11-21T10:05:00+08:00",
                                      "end_time": "2026-11-21T10:15:00+08:00",
                                      "serial_no": "REP-20261121-0001"})
    client.post("/api/publications")
    events = client.get("/api/events").json()["events"]
    detail = client.get(f"/api/events/{events[0]['event_id']}").json()
    assert [a["serial_no"] for a in detail["alarms"]] == ["ALM-20261121-0001"]
    assert [r["serial_no"] for r in detail["repairs"]] == ["REP-20261121-0001"]


# ---------------------------------------------------------------- 日档（Swagger 演示主线）

NOTICE = {"plan_no": "PLAN-20261121-001", "feeder_no": "F01", "kind": "notice",
          "announced_at": "2026-11-20T18:00:00+08:00",
          "planned_start": "2026-11-21T09:30:00+08:00", "planned_end": "2026-11-21T10:30:00+08:00"}
CANCEL = {"plan_no": "PLAN-20261121-001", "feeder_no": "F01", "kind": "cancel",
          "announced_at": "2026-11-21T09:00:00+08:00"}


def _closed_publication(client):
    """一个已闭合事件：10:00-11:00 (+08:00) = 02:00-03:00 UTC。"""
    client.post("/api/alarms", json={"feeder_no": "F01", "device_no": "D01",
                                     "start_time": "2026-11-21T10:00:00+08:00",
                                     "end_time": "2026-11-21T10:20:00+08:00",
                                     "serial_no": "ALM-DAILY-0001"})
    client.post("/api/restorations", json={"feeder_no": "F01", "device_no": "D01",
                                           "restore_time": "2026-11-21T11:00:00+08:00",
                                           "serial_no": "RST-DAILY-0001"})
    return client.post("/api/publications").json()["publication_id"]


def test_daily_archive_swagger_flow(client):
    """补录撤销 -> 日档随入库停供 -> 重算恢复供数 -> 旧日档按号回查。"""
    pub = _closed_publication(client)
    assert client.post("/api/plan-notices", json=NOTICE).json()["created"] is True

    # 核算：计划覆盖 09:30-10:30(+08:00)，事件 10:00-11:00 -> 计划内 30 分钟 + 计划外 30 分钟
    a1 = client.post("/api/daily-archives",
                     json={"publication_id": pub, "feeder_no": "F01", "date": "2026-11-21"}).json()
    assert a1["status"] == "serving" and a1["previous_status"] == "none"
    assert a1["planned_minutes"] == 30.0 and a1["unplanned_minutes"] == 30.0
    assert a1["segments"][0]["basis"][0]["plan_no"] == "PLAN-20261121-001"

    # 补录撤销（公告时刻 09:00+08:00，早于停电开始）-> 随入库停供
    r = client.post("/api/plan-notices", json=CANCEL).json()
    assert r["created"] is True
    assert [s["archive_id"] for s in r["staled_archives"]] == [a1["archive_id"]]
    cur = client.get(f"/api/daily-archives/current?publication_id={pub}"
                     f"&feeder_no=F01&date=2026-11-21").json()
    assert cur["status"] == "stale"

    # 撤销原样重送：幂等，只记一次，不重复停供
    again = client.post("/api/plan-notices", json=CANCEL).json()
    assert again["created"] is False and again["staled_archives"] == []

    # 重算成功才恢复供数：末条为撤销，计划不计，整段计划外
    a2 = client.post("/api/daily-archives",
                     json={"publication_id": pub, "feeder_no": "F01", "date": "2026-11-21"}).json()
    assert a2["status"] == "serving" and a2["previous_status"] == "stale"
    assert a2["planned_minutes"] == 0.0 and a2["unplanned_minutes"] == 60.0
    cur = client.get(f"/api/daily-archives/current?publication_id={pub}"
                     f"&feeder_no=F01&date=2026-11-21").json()
    assert cur["status"] == "serving" and cur["archive"]["archive_id"] == a2["archive_id"]

    # 旧日档按号回查：停供的旧档仍保留原分段与依据
    old = client.get(f"/api/daily-archives/{a1['archive_id']}").json()
    assert old["status"] == "stale" and old["planned_minutes"] == 30.0
    assert old["segments"][0]["kind"] == "planned"


def test_plan_notice_conflict_409(client):
    assert client.post("/api/plan-notices", json=NOTICE).status_code == 200
    conflict = dict(NOTICE, planned_end="2026-11-21T11:00:00+08:00")  # 同键异文
    resp = client.post("/api/plan-notices", json=conflict)
    assert resp.status_code == 409
    other_feeder = dict(NOTICE, feeder_no="F02",
                        announced_at="2026-11-20T19:00:00+08:00")      # 同计划异馈线
    assert client.post("/api/plan-notices", json=other_feeder).status_code == 409


def test_plan_notice_validation_422(client):
    missing = {"plan_no": "P1", "feeder_no": "F01", "kind": "notice",
               "announced_at": "2026-11-20T18:00:00+08:00"}            # notice 缺计划起止
    assert client.post("/api/plan-notices", json=missing).status_code == 422
    naive = dict(NOTICE, announced_at="2026-11-20T18:00:00")           # 缺时区
    assert client.post("/api/plan-notices", json=naive).status_code == 422


def test_daily_archive_requires_success_publication(client):
    resp = client.post("/api/daily-archives",
                       json={"publication_id": 999, "feeder_no": "F01", "date": "2026-11-21"})
    assert resp.status_code == 404


def test_daily_archive_current_none_before_accounting(client):
    pub = _closed_publication(client)
    cur = client.get(f"/api/daily-archives/current?publication_id={pub}"
                     f"&feeder_no=F01&date=2026-11-21").json()
    assert cur["status"] == "none" and cur["archive"] is None
    assert client.get("/api/daily-archives/999").status_code == 404
