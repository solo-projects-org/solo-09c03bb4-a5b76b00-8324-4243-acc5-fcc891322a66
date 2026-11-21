"""日档核算：计划公告登记、计划内/计划外分段、晚录停供与重算恢复。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

from app import service
from app.db import init_db

UTC = timezone.utc
DAY = date(2026, 11, 21)


def utc(hour: int, minute: int = 0, day: int = 21) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTAGE_DB_PATH", str(tmp_path / "daily.db"))
    monkeypatch.setenv("MERGE_GAP_MINUTES", "5")
    init_db()
    yield


def publish_closed_event(feeder="F01", start=utc(2, 0), restore=utc(3, 0), tag="1"):
    """造一个已闭合事件（一条告警 + 一条有效回执）并发布，返回发布号。"""
    service.ingest("alarm", feeder_no=feeder, device_no="D01",
                   start_time=start, end_time=start + timedelta(minutes=20),
                   serial_no=f"ALM-{tag}")
    service.ingest("restoration", feeder_no=feeder, device_no="D01",
                   start_time=restore, end_time=restore, serial_no=f"RST-{tag}")
    return service.publish()["publication_id"]


def notice(plan="PLAN-1", feeder="F01", announced=utc(12, 0, day=20),
           start=utc(1, 30), end=utc(2, 30), kind="notice"):
    return service.register_plan_notice(
        plan_no=plan, feeder_no=feeder, announced_at=announced,
        planned_start=start, planned_end=end, kind=kind)


def account(pub, feeder="F01", day=DAY):
    return service.account_daily_archive(pub, feeder, day)


def current(pub, feeder="F01", day=DAY):
    return service.current_daily_archive(pub, feeder, day)


# ---------------------------------------------------------------- 公告登记

def test_notice_register_and_identical_resend_recorded_once():
    r1 = notice()
    assert r1["created"] is True and r1["consistent"] is True
    r2 = notice()  # 同计划同公告时刻原样重送
    assert r2["created"] is False and r2["consistent"] is True
    assert r2["record"]["notice_id"] == r1["record"]["notice_id"]
    assert len(service.list_plan_notices("PLAN-1")) == 1  # 只记一次


def test_conflicting_notice_rejected():
    notice(end=utc(2, 30))
    with pytest.raises(service.PlanNoticeConflict):
        notice(end=utc(2, 45))  # 同计划同公告时刻，异文
    assert len(service.list_plan_notices("PLAN-1")) == 1  # 异文未入库


def test_same_plan_fixed_feeder():
    notice(plan="PLAN-1", feeder="F01")
    with pytest.raises(service.PlanNoticeConflict):
        notice(plan="PLAN-1", feeder="F02", announced=utc(13, 0, day=20))
    # 不同计划可以用别的馈线
    r = notice(plan="PLAN-2", feeder="F02", announced=utc(13, 0, day=20))
    assert r["created"] is True


def test_notice_kind_validation():
    with pytest.raises(ValueError):
        notice(kind="notice", start=None, end=None)  # notice 缺计划起止
    with pytest.raises(ValueError):
        notice(start=utc(2, 30), end=utc(1, 30))     # 计划止早于起
    with pytest.raises(ValueError):
        notice(kind="other")


# ---------------------------------------------------------------- 分段核算

def test_segments_split_planned_and_unplanned_with_basis():
    pub = publish_closed_event()                     # 事件 02:00-03:00 UTC
    notice(start=utc(1, 30), end=utc(2, 30))         # 计划覆盖 01:30-02:30
    arch = account(pub)
    assert arch["status"] == "serving"
    assert arch["event_count"] == 1
    assert arch["planned_minutes"] == 30.0
    assert arch["unplanned_minutes"] == 30.0
    kinds = [(s["kind"], s["seg_start"], s["seg_end"]) for s in arch["segments"]]
    assert kinds == [
        ("planned", service.to_store(utc(2, 0)), service.to_store(utc(2, 30))),
        ("unplanned", service.to_store(utc(2, 30)), service.to_store(utc(3, 0))),
    ]
    planned_seg = arch["segments"][0]
    assert [b["plan_no"] for b in planned_seg["basis"]] == ["PLAN-1"]
    assert planned_seg["basis"][0]["announced_at"] == service.to_store(utc(12, 0, day=20))
    assert arch["segments"][1]["basis"] == []        # 计划外段无通知依据


def test_overlapping_plans_counted_once():
    pub = publish_closed_event()                     # 02:00-03:00
    notice(plan="PLAN-1", start=utc(1, 30), end=utc(2, 30))
    notice(plan="PLAN-2", start=utc(2, 0), end=utc(3, 30))   # 与 PLAN-1 重叠 02:00-02:30
    arch = account(pub)
    assert arch["planned_minutes"] == 60.0           # 重叠覆盖只计一次
    assert arch["unplanned_minutes"] == 0.0
    overlap = [s for s in arch["segments"]
               if s["seg_start"] == service.to_store(utc(2, 0))
               and s["seg_end"] == service.to_store(utc(2, 30))]
    assert len(overlap) == 1
    assert sorted(b["plan_no"] for b in overlap[0]["basis"]) == ["PLAN-1", "PLAN-2"]


def test_last_notice_before_event_start_governs():
    pub = publish_closed_event()                     # 事件开始 02:00
    notice(plan="PLAN-1", announced=utc(10, 0, day=20), start=utc(1, 0), end=utc(2, 0))
    notice(plan="PLAN-1", announced=utc(20, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    arch = account(pub)
    # 停电开始时已公告的最后一条（20 日 20:00 那条）决定覆盖范围
    assert arch["planned_minutes"] == 30.0
    basis = arch["segments"][0]["basis"]
    assert basis[0]["announced_at"] == service.to_store(utc(20, 0, day=20))


def test_notice_announced_after_event_start_ignored():
    pub = publish_closed_event()                     # 事件开始 02:00
    notice(announced=utc(2, 0, day=21).replace(minute=1))  # 02:01 才公告
    arch = account(pub)
    assert arch["planned_minutes"] == 0.0
    assert arch["unplanned_minutes"] == 60.0


def test_notice_announced_exactly_at_event_start_counts():
    pub = publish_closed_event()
    notice(announced=utc(2, 0, day=21))              # 恰在停电开始时已公告
    arch = account(pub)
    assert arch["planned_minutes"] == 30.0


def test_plan_whose_last_notice_is_cancel_not_counted():
    pub = publish_closed_event()
    notice(plan="PLAN-1", announced=utc(10, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    notice(plan="PLAN-1", announced=utc(1, 0, day=21), kind="cancel",
           start=None, end=None)                     # 末条为撤销
    arch = account(pub)
    assert arch["planned_minutes"] == 0.0
    assert arch["unplanned_minutes"] == 60.0
    assert all(s["kind"] == "unplanned" for s in arch["segments"])


def test_cancel_then_renotice_counts():
    pub = publish_closed_event()
    notice(plan="PLAN-1", announced=utc(10, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    notice(plan="PLAN-1", announced=utc(11, 0, day=20), kind="cancel", start=None, end=None)
    notice(plan="PLAN-1", announced=utc(12, 0, day=20), start=utc(2, 0), end=utc(3, 0))
    arch = account(pub)
    assert arch["planned_minutes"] == 60.0           # 撤销后再公告，以末条公告为准


def test_only_closed_events_accounted():
    service.ingest("alarm", feeder_no="F01", device_no="D01",
                   start_time=utc(2, 0), end_time=utc(2, 20), serial_no="ALM-X")
    pub = service.publish()["publication_id"]        # 未闭合事件
    arch = account(pub)
    assert arch["event_count"] == 0 and arch["segments"] == []


def test_event_spanning_midnight_split_by_day():
    # 事件 23:00(20日) - 01:00(21日) UTC
    pub = publish_closed_event(start=utc(23, 0, day=20), restore=utc(1, 0, day=21))
    notice(start=utc(22, 30, day=20), end=utc(0, 30, day=21))
    day20 = account(pub, day=date(2026, 11, 20))
    day21 = account(pub, day=DAY)
    assert day20["planned_minutes"] == 60.0 and day20["unplanned_minutes"] == 0.0
    assert day21["planned_minutes"] == 30.0 and day21["unplanned_minutes"] == 30.0
    assert all(s["seg_start"] >= "2026-11-20T00:00:00" for s in day20["segments"])
    assert all(s["seg_end"] <= "2026-11-22T00:00:00" for s in day21["segments"])


def test_account_requires_success_publication():
    with pytest.raises(LookupError):
        account(999)                                 # 发布不存在
    service.ingest("alarm", feeder_no="F01", device_no="D01",
                   start_time=utc(2, 0), end_time=utc(2, 20), serial_no="ALM-P")
    service.publish()
    service._record_failed_publication(RuntimeError("模拟"))  # 造一条失败发布
    failed_id = service.list_publications()[0]["publication_id"]
    with pytest.raises(ValueError):
        account(failed_id)                           # 失败发布不能核算


def test_invalid_date_rejected():
    pub = publish_closed_event()
    with pytest.raises(ValueError):
        account(pub, day="2026-11-21T10:00:00")
    with pytest.raises(ValueError):
        account(pub, day="not-a-date")


# ---------------------------------------------------------------- 晚录停供与重算恢复

def test_late_notice_stales_archive_and_recompute_restores():
    """Swagger 演示主线：补录撤销 -> 日档随入库停供 -> 重算成功恢复供数。"""
    pub = publish_closed_event()                     # 事件 02:00-03:00 UTC
    notice(plan="PLAN-1", announced=utc(12, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    a1 = account(pub)
    assert a1["planned_minutes"] == 30.0 and a1["previous_status"] == "none"
    assert current(pub)["status"] == "serving"

    # 晚录撤销（公告时刻 01:00，仍早于停电开始 02:00）-> 随入库停供
    r = notice(plan="PLAN-1", announced=utc(1, 0, day=21), kind="cancel",
               start=None, end=None)
    assert r["created"] is True
    assert [s["archive_id"] for s in r["staled_archives"]] == [a1["archive_id"]]
    cur = current(pub)
    assert cur["status"] == "stale"                  # 停供，待重算
    assert cur["archive"]["archive_id"] == a1["archive_id"]

    # 重算成功才恢复供数：末条为撤销，计划不计，整段计划外
    a2 = account(pub)
    assert a2["status"] == "serving"
    assert a2["previous_status"] == "stale"
    assert a2["planned_minutes"] == 0.0 and a2["unplanned_minutes"] == 60.0
    assert current(pub)["status"] == "serving"
    assert current(pub)["archive"]["archive_id"] == a2["archive_id"]

    # 旧日档按号回查：停供的旧档仍保留原分段
    old = service.get_daily_archive(a1["archive_id"])
    assert old["status"] == "stale"
    assert old["planned_minutes"] == 30.0
    assert old["segments"][0]["kind"] == "planned"


def test_late_notice_without_effect_keeps_serving():
    pub = publish_closed_event()
    notice(plan="PLAN-1", announced=utc(12, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    account(pub)
    # 公告时刻晚于所有事件开始 -> 不改变分段或依据，不停供
    r = notice(plan="PLAN-9", announced=utc(4, 0, day=21), start=utc(4, 30), end=utc(5, 0))
    assert r["staled_archives"] == []
    assert current(pub)["status"] == "serving"
    # 被更晚公告覆盖的旧时刻公告 -> 也不会成为末条，不停供
    r2 = notice(plan="PLAN-1", announced=utc(11, 0, day=20), start=utc(1, 0), end=utc(3, 0))
    assert r2["staled_archives"] == []
    assert current(pub)["status"] == "serving"


def test_late_notice_for_other_feeder_not_affected():
    pub = publish_closed_event(feeder="F01")
    notice(plan="PLAN-1", feeder="F01", announced=utc(12, 0, day=20),
           start=utc(1, 30), end=utc(2, 30))
    account(pub, feeder="F01")
    r = notice(plan="PLAN-2", feeder="F02", announced=utc(1, 0, day=21),
               start=utc(1, 0), end=utc(3, 0))
    assert r["staled_archives"] == []
    assert current(pub, feeder="F01")["status"] == "serving"


def test_identical_resend_does_not_restale():
    pub = publish_closed_event()
    notice(plan="PLAN-1", announced=utc(12, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    account(pub)
    notice(plan="PLAN-1", announced=utc(1, 0, day=21), kind="cancel",
           start=None, end=None)                     # 停供
    account(pub)                                     # 重算恢复
    again = notice(plan="PLAN-1", announced=utc(1, 0, day=21), kind="cancel",
                   start=None, end=None)             # 原样重送
    assert again["created"] is False and again["staled_archives"] == []
    assert current(pub)["status"] == "serving"


def test_failed_recompute_preserves_serving_state():
    """写入失败保留此前供数状态，并记一条 failed 日档留痕。"""
    pub = publish_closed_event()
    notice(plan="PLAN-1", announced=utc(12, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    a1 = account(pub)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写入失败")

    with mock.patch.object(service, "_write_daily_archive", boom):
        with pytest.raises(RuntimeError):
            account(pub)
    cur = current(pub)
    assert cur["status"] == "serving"                # 供数状态不变
    assert cur["archive"]["archive_id"] == a1["archive_id"]
    failed = service.list_daily_archives(status="failed")
    assert len(failed) == 1 and "模拟写入失败" in failed[0]["error"]

    a2 = account(pub)                                # 恢复后可正常重算
    assert a2["status"] == "serving" and a2["archive_id"] != a1["archive_id"]
    assert service.get_daily_archive(a1["archive_id"])["status"] == "superseded"


def test_failed_recompute_keeps_stale_state():
    """停供状态下重算失败：仍然停供，不恢复供数。"""
    pub = publish_closed_event()
    notice(plan="PLAN-1", announced=utc(12, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    account(pub)
    notice(plan="PLAN-1", announced=utc(1, 0, day=21), kind="cancel",
           start=None, end=None)                     # 停供
    assert current(pub)["status"] == "stale"

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写入失败")

    with mock.patch.object(service, "_write_daily_archive", boom):
        with pytest.raises(RuntimeError):
            account(pub)
    assert current(pub)["status"] == "stale"         # 重算失败不恢复

    account(pub)                                     # 重算成功才恢复
    assert current(pub)["status"] == "serving"


# ---------------------------------------------------------------- 补录与重算交错

def test_interleaved_ingest_and_recompute():
    """补录与重算交错：每次晚录公告立即反映到供数状态，重算后依据最新入库公告。"""
    pub = publish_closed_event()                     # 事件 02:00-03:00 UTC
    notice(plan="PLAN-1", announced=utc(10, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    a1 = account(pub)
    assert (a1["planned_minutes"], a1["unplanned_minutes"]) == (30.0, 30.0)

    # 交错 1：补录第二个计划（覆盖后半段）-> 停供 -> 重算 -> 全部计划内
    notice(plan="PLAN-2", announced=utc(11, 0, day=20), start=utc(2, 30), end=utc(3, 30))
    assert current(pub)["status"] == "stale"
    a2 = account(pub)
    assert (a2["planned_minutes"], a2["unplanned_minutes"]) == (60.0, 0.0)
    assert current(pub)["status"] == "serving"

    # 交错 2：补录一条不影响分段的公告（公告时刻晚于事件开始）-> 不停供
    notice(plan="PLAN-3", announced=utc(5, 0, day=21), start=utc(5, 30), end=utc(6, 0))
    assert current(pub)["status"] == "serving"

    # 交错 3：补录撤销 PLAN-1 -> 停供 -> 重算 -> 只剩 PLAN-2 的覆盖
    notice(plan="PLAN-1", announced=utc(1, 0, day=21), kind="cancel",
           start=None, end=None)
    assert current(pub)["status"] == "stale"
    a3 = account(pub)
    assert (a3["planned_minutes"], a3["unplanned_minutes"]) == (30.0, 30.0)
    kinds = [(s["kind"], s["seg_start"], s["seg_end"]) for s in a3["segments"]]
    assert kinds == [
        ("unplanned", service.to_store(utc(2, 0)), service.to_store(utc(2, 30))),
        ("planned", service.to_store(utc(2, 30)), service.to_store(utc(3, 0))),
    ]
    assert [b["plan_no"] for b in a3["segments"][1]["basis"]] == ["PLAN-2"]

    # 交错 4：连续补录两条后才重算 -> 一次重算纳入全部已入库公告
    notice(plan="PLAN-2", announced=utc(1, 30, day=21), kind="cancel",
           start=None, end=None)
    notice(plan="PLAN-1", announced=utc(1, 40, day=21), start=utc(2, 0), end=utc(2, 15))
    assert current(pub)["status"] == "stale"
    a4 = account(pub)
    # PLAN-1 末条为新公告（覆盖 02:00-02:15），PLAN-2 末条为撤销
    assert (a4["planned_minutes"], a4["unplanned_minutes"]) == (15.0, 45.0)
    assert current(pub)["status"] == "serving"

    # 全部旧日档可按号回查，状态各异
    assert service.get_daily_archive(a1["archive_id"])["status"] == "stale"
    assert service.get_daily_archive(a2["archive_id"])["status"] == "stale"
    assert service.get_daily_archive(a3["archive_id"])["status"] == "stale"
    assert service.get_daily_archive(a4["archive_id"])["status"] == "serving"


def test_archive_pins_notice_version_at_effective_time():
    """日档依据生效时已入库的公告：核算后入库的公告不改变旧日档内容。"""
    pub = publish_closed_event()
    notice(plan="PLAN-1", announced=utc(12, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    a1 = account(pub)
    assert a1["notice_version"] == 1
    notice(plan="PLAN-2", announced=utc(13, 0, day=20), start=utc(2, 30), end=utc(3, 30))
    a2 = account(pub)
    assert a2["notice_version"] == 2                 # 重算依据最新入库公告
    old = service.get_daily_archive(a1["archive_id"])
    assert old["notice_version"] == 1                # 旧档内容不变
    assert old["planned_minutes"] == 30.0
    assert a2["planned_minutes"] == 60.0


def test_superseded_archive_queryable_by_id():
    pub = publish_closed_event()
    notice(plan="PLAN-1", announced=utc(12, 0, day=20), start=utc(1, 30), end=utc(2, 30))
    a1 = account(pub)
    a2 = account(pub)                                # 再次核算，a1 被取代
    assert a2["superseded_archive_id"] == a1["archive_id"]
    assert a2["previous_status"] == "serving"
    old = service.get_daily_archive(a1["archive_id"])
    assert old["status"] == "superseded"
    assert current(pub)["archive"]["archive_id"] == a2["archive_id"]
