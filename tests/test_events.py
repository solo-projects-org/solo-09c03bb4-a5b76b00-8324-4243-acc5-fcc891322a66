"""事件归并/闭合/版本化发布 的单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import service
from app.db import init_db

CST = timezone(timedelta(hours=8))


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 21, hour, minute, tzinfo=CST)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTAGE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MERGE_GAP_MINUTES", "5")
    init_db()
    yield


def add_alarm(feeder="F01", device="D01", start=dt(10, 0), end=dt(10, 20), serial="ALM-1"):
    return service.ingest("alarm", feeder_no=feeder, device_no=device,
                          start_time=start, end_time=end, serial_no=serial)


def add_receipt(feeder="F01", device="D01", restore=dt(10, 30), serial="RST-1"):
    return service.ingest("restoration", feeder_no=feeder, device_no=device,
                          start_time=restore, end_time=restore, serial_no=serial)


def events_of(publication_id=None, feeder_no=None):
    return service.list_events(publication_id, feeder_no)["events"]


# ---------------------------------------------------------------- 归并

def test_merge_gap_within_5min():
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 20), device="D01")
    add_alarm(serial="ALM-2", start=dt(10, 25), end=dt(10, 45), device="D02")  # 间隔正好 5 分钟
    service.publish()
    events = events_of()
    assert len(events) == 1
    ev = events[0]
    assert ev["closed"] is False
    assert ev["unclosed_reason"] == service.UNCLOSED_REASON_NO_RECEIPT
    assert ev["devices"] == ["D01", "D02"]
    assert ev["start_time"] == service.to_store(dt(10, 0))
    assert ev["end_time"] == service.to_store(dt(10, 45))


def test_no_merge_gap_over_5min():
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 20))
    add_alarm(serial="ALM-2", start=dt(10, 26), end=dt(10, 40))  # 间隔 6 分钟
    service.publish()
    assert len(events_of()) == 2


def test_intersecting_alarms_merge():
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 30))
    add_alarm(serial="ALM-2", start=dt(10, 10), end=dt(10, 50))  # 相交
    service.publish()
    events = events_of()
    assert len(events) == 1
    assert events[0]["end_time"] == service.to_store(dt(10, 50))


def test_different_feeders_not_merged():
    add_alarm(serial="ALM-1", feeder="F01")
    add_alarm(serial="ALM-2", feeder="F02")
    service.publish()
    assert len(events_of()) == 2


# ---------------------------------------------------------------- 闭合

def test_valid_receipt_closes_event():
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 20))
    add_receipt(serial="RST-1", restore=dt(10, 30))
    service.publish()
    events = events_of()
    assert len(events) == 1
    ev = events[0]
    assert ev["closed"] is True
    assert ev["closed_by_serial"] == "RST-1"
    assert ev["unclosed_reason"] is None
    assert ev["end_time"] == service.to_store(dt(10, 30))  # 结束时间取复电时刻


def test_alarms_after_receipt_belong_to_next_event():
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 20))
    add_receipt(serial="RST-1", restore=dt(10, 30))
    add_alarm(serial="ALM-2", start=dt(10, 35), end=dt(10, 40))  # 回执之后
    service.publish()
    events = events_of()
    assert len(events) == 2
    assert events[0]["closed"] is True
    assert events[1]["closed"] is False
    assert events[1]["unclosed_reason"] == service.UNCLOSED_REASON_NO_RECEIPT


def test_receipt_before_event_start_is_unapplied():
    add_receipt(serial="RST-0", restore=dt(9, 0))   # 比任何告警都早
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 20))
    result = service.publish()
    events = events_of()
    assert len(events) == 1 and events[0]["closed"] is False
    assert result["unapplied_receipts"][0]["serial_no"] == "RST-0"


# ---------------------------------------------------------------- 迟到更正：拆分 / 合并 / 回溯

def test_backfilled_receipt_splits_event_and_old_publication_kept():
    """Swagger 演示场景 1：补录复电回执导致事件拆分，旧发布号仍可查更正前结果。"""
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 20), device="D01")
    add_alarm(serial="ALM-2", start=dt(10, 25), end=dt(10, 45), device="D02")
    p1 = service.publish()
    assert len(events_of(p1["publication_id"])) == 1  # 更正前：一个未闭合大事件

    add_receipt(serial="RST-1", restore=dt(10, 22))   # 迟到补录的回执
    p2 = service.publish()
    events = events_of(p2["publication_id"])
    assert len(events) == 2                            # 拆成两个事件
    first, second = events
    assert first["closed"] is True
    assert first["end_time"] == service.to_store(dt(10, 22))
    assert first["closed_by_serial"] == "RST-1"
    assert first["devices"] == ["D01"]
    assert second["closed"] is False
    assert second["devices"] == ["D02"]
    assert second["start_time"] == service.to_store(dt(10, 25))

    old = events_of(p1["publication_id"])              # 旧发布号回溯更正前结果
    assert len(old) == 1 and old[0]["closed"] is False


def test_late_alarm_merges_events():
    """迟到更正场景 2：补录的告警把两个事件桥接合并。"""
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 20))
    add_alarm(serial="ALM-2", start=dt(10, 30), end=dt(10, 40))  # 间隔 10 分钟 -> 两个事件
    p1 = service.publish()
    assert len(events_of(p1["publication_id"])) == 2

    add_alarm(serial="ALM-3", start=dt(10, 21), end=dt(10, 29))  # 迟到补录，桥接
    p2 = service.publish()
    events = events_of(p2["publication_id"])
    assert len(events) == 1
    assert events[0]["start_time"] == service.to_store(dt(10, 0))
    assert events[0]["end_time"] == service.to_store(dt(10, 40))
    assert len(events_of(p1["publication_id"])) == 2  # 旧发布不变


# ---------------------------------------------------------------- 幂等重送

def test_receipt_resend_is_idempotent():
    """Swagger 演示场景 2：回执重送（同流水号）幂等，不改变已发布结果。"""
    add_alarm(serial="ALM-1", start=dt(10, 0), end=dt(10, 20))
    r1 = add_receipt(serial="RST-1", restore=dt(10, 30))
    assert r1["created"] is True
    p1 = service.publish()

    r2 = add_receipt(serial="RST-1", restore=dt(10, 30))  # 重送同一回执
    assert r2["created"] is False
    assert r2["consistent"] is True
    p2 = service.publish()

    e1, e2 = events_of(p1["publication_id"]), events_of(p2["publication_id"])
    assert [(e["start_time"], e["end_time"], e["closed"]) for e in e1] == \
           [(e["start_time"], e["end_time"], e["closed"]) for e in e2]


def test_alarm_resend_is_idempotent():
    add_alarm(serial="ALM-1")
    again = add_alarm(serial="ALM-1")
    assert again["created"] is False
    service.publish()
    assert len(events_of()) == 1


def test_same_serial_different_content_flagged_inconsistent():
    add_receipt(serial="RST-1", restore=dt(10, 30))
    conflict = add_receipt(serial="RST-1", restore=dt(10, 35))  # 同流水号不同内容
    assert conflict["created"] is False
    assert conflict["consistent"] is False


# ---------------------------------------------------------------- 版本固定与失败回落

def test_publication_pins_input_versions():
    add_alarm(serial="ALM-1")
    add_alarm(serial="ALM-2", start=dt(11, 0), end=dt(11, 10))
    add_receipt(serial="RST-1", restore=dt(10, 30))
    service.ingest("repair", feeder_no="F01", device_no="D01",
                   start_time=dt(10, 5), end_time=dt(10, 25), serial_no="REP-1")
    result = service.publish()
    assert result["versions"] == {"alarm": 2, "repair": 1, "restoration": 1}
    pub = service.publication_detail(result["publication_id"])["publication"]
    assert pub["versions"] == {"alarm": 2, "repair": 1, "restoration": 1}


def test_failed_publish_keeps_previous_version(monkeypatch):
    add_alarm(serial="ALM-1")
    p1 = service.publish()

    def boom(conn):
        raise RuntimeError("模拟计算失败")

    monkeypatch.setattr(service, "_compute_events", boom)
    with pytest.raises(RuntimeError):
        service.publish()

    pubs = service.list_publications()
    assert pubs[0]["status"] == "failed" and "模拟计算失败" in pubs[0]["error"]
    # 查询仍回落到上一成功发布
    fallback = service.list_events(None, None)
    assert fallback["publication"]["publication_id"] == p1["publication_id"]
    assert len(fallback["events"]) == 1


def test_no_successful_publication_yet():
    with pytest.raises(LookupError):
        service.list_events(None, None)
