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
