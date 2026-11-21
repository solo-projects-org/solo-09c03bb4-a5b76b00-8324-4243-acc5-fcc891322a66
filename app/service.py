"""核心业务：输入入库（幂等）、事件归并/闭合计算、版本化发布、日档核算。

规则：
- 同一馈线的告警区间相交，或间隔不超过 MERGE_GAP_MINUTES（默认 5）分钟，归并为同一事件；
- 有效复电回执（复电时刻 >= 事件开始时刻）闭合事件，事件结束时间取复电时刻；
- 回执之后的告警归入后续事件；
- 无有效回执的事件保持未闭合，并给出未闭合原因；
- 每次发布固定三类输入（告警/抢修/复电回执）的版本；发布失败时记录 failed 发布，
  查询继续回落到上一成功发布；旧发布号永远可查更正前的结果。

计划公告与日档：
- 计划公告按计划号登记（馈线、实际公告时刻、计划起止，或撤销）；同一计划固定馈线；
  (plan_no, announced_at) 为幂等键：原样重送只记一次，异文拒收；
- 日档按 成功发布号+馈线+UTC日期 核算已闭合事件：对每个事件取各计划在停电开始时
  已公告的最后一条通知（末条为撤销的计划不计），把事件落在当天的部分切成
  计划内/计划外段，重叠覆盖只计一次，逐段记录通知依据；
- 晚录公告若改变某供数中日档的分段或依据，该日档随入库停供（stale），
  重算成功才恢复供数；旧日档（停供/被取代）均可按日档号回查；
- 日档依据生效时已入库的公告（notice_version 固定）；核算写入失败时整体回滚，
  保留此前供数状态，并另记一条 failed 日档留痕。
"""
from __future__ import annotations

import json
import os
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .db import get_conn

UNCLOSED_REASON_NO_RECEIPT = "未收到有效复电回执"
UNAPPLIED_NO_OPEN_EVENT = "无对应未闭合事件"
UNAPPLIED_BEFORE_EVENT_START = "回执复电时刻早于事件开始"

INPUT_TABLES = ("alarms", "repairs", "restorations")
_KIND_TO_TABLE = {"alarm": "alarms", "repair": "repairs", "restoration": "restorations"}


def merge_gap() -> timedelta:
    return timedelta(minutes=float(os.environ.get("MERGE_GAP_MINUTES", "5")))


def utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def to_store(dt: datetime) -> str:
    """统一转成 UTC 定宽 ISO 字符串，保证字典序 == 时间序。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_store(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------- 入库

def ingest(kind: str, *, feeder_no: str, device_no: str, start_time: datetime,
           end_time: datetime, serial_no: str) -> dict[str, Any]:
    """写入一条输入记录。serial_no 唯一：重发返回 created=False，不重复入库。

    consistent=False 表示同流水号但内容与已存记录不一致（调用方应核查）。
    """
    table = _KIND_TO_TABLE[kind]
    start_s, end_s = to_store(start_time), to_store(end_time)
    with get_conn() as conn:
        cur = conn.execute(
            f"""INSERT OR IGNORE INTO {table}
                (feeder_no, device_no, start_time, end_time, serial_no, received_at)
                VALUES (?,?,?,?,?,?)""",
            (feeder_no, device_no, start_s, end_s, serial_no, utc_now_str()),
        )
        created = cur.rowcount == 1
        row = conn.execute(f"SELECT * FROM {table} WHERE serial_no = ?", (serial_no,)).fetchone()
    record = dict(row)
    consistent = (
        record["feeder_no"] == feeder_no
        and record["device_no"] == device_no
        and record["start_time"] == start_s
        and record["end_time"] == end_s
    )
    record["kind"] = kind
    return {"created": created, "consistent": consistent, "record": record}


# ---------------------------------------------------------------- 计算

def _input_versions(conn) -> dict[str, int]:
    """三类输入的版本：各自表内最大 id（输入只增不改，故单调递增）。"""
    return {
        t: conn.execute(f"SELECT COALESCE(MAX(id),0) AS v FROM {t}").fetchone()["v"]
        for t in INPUT_TABLES
    }


def _new_event(feeder: str, alarm: dict) -> dict:
    return {
        "feeder_no": feeder,
        "start": from_store(alarm["start_time"]),
        "end": from_store(alarm["end_time"]),
        "closed": False,
        "closed_by": None,
        "unclosed_reason": None,
        "devices": [alarm["device_no"]],
        "alarms": [alarm],
    }


def _extend_event(event: dict, alarm: dict, gap: timedelta) -> bool:
    """告警与当前事件相交或间隔不超过 gap 时归并，返回是否归并。"""
    a_start = from_store(alarm["start_time"])
    a_end = from_store(alarm["end_time"])
    if a_start > event["end"] + gap:
        return False
    if a_end > event["end"]:
        event["end"] = a_end
    if alarm["device_no"] not in event["devices"]:
        event["devices"].append(alarm["device_no"])
    event["alarms"].append(alarm)
    return True


def _compute_events(conn) -> tuple[list[dict], list[dict]]:
    """按馈线归并告警、用有效回执闭合事件。返回 (事件列表, 未生效回执列表)。"""
    gap = merge_gap()
    feeders = [
        r["feeder_no"]
        for r in conn.execute(
            "SELECT feeder_no FROM alarms UNION SELECT feeder_no FROM restorations "
            "ORDER BY feeder_no"
        )
    ]
    events: list[dict] = []
    unapplied: list[dict] = []

    for feeder in feeders:
        alarms = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM alarms WHERE feeder_no=? ORDER BY start_time, id", (feeder,)
            )
        ]
        receipts = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM restorations WHERE feeder_no=? ORDER BY start_time, id", (feeder,)
            )
        ]

        cur: Optional[dict] = None
        ri = 0

        def absorb(receipt: dict) -> None:
            """回执有效（复电时刻 >= 事件开始）则闭合当前事件，否则记为未生效。"""
            nonlocal cur
            r_start = from_store(receipt["start_time"])
            if cur is not None and r_start >= cur["start"]:
                cur["closed"] = True
                cur["end"] = r_start
                cur["closed_by"] = receipt["serial_no"]
                events.append(cur)
                cur = None
            else:
                unapplied.append({
                    "serial_no": receipt["serial_no"],
                    "feeder_no": feeder,
                    "restore_time": receipt["start_time"],
                    "reason": UNAPPLIED_NO_OPEN_EVENT if cur is None
                              else UNAPPLIED_BEFORE_EVENT_START,
                })

        for alarm in alarms:
            a_start = from_store(alarm["start_time"])
            # 先吸收复电时刻不晚于该告警开始的所有回执：回执之后的告警归入后续事件
            while ri < len(receipts) and from_store(receipts[ri]["start_time"]) <= a_start:
                absorb(receipts[ri])
                ri += 1
            if cur is None:
                cur = _new_event(feeder, alarm)
            elif not _extend_event(cur, alarm, gap):
                cur["unclosed_reason"] = UNCLOSED_REASON_NO_RECEIPT
                events.append(cur)
                cur = _new_event(feeder, alarm)

        while ri < len(receipts):
            absorb(receipts[ri])
            ri += 1
        if cur is not None:
            cur["unclosed_reason"] = UNCLOSED_REASON_NO_RECEIPT
            events.append(cur)

    return events, unapplied


# ---------------------------------------------------------------- 发布

def publish() -> dict[str, Any]:
    """生成一次发布：固定三类输入版本，重算全部事件（整体一个事务）。

    失败时另记一条 failed 发布（审计用）并抛出异常；查询侧始终回落到
    最新 success 发布，因此失败不影响线上已提供的结果。
    """
    try:
        with get_conn() as conn:
            versions = _input_versions(conn)
            events, unapplied = _compute_events(conn)
            cur = conn.execute(
                """INSERT INTO publications
                   (created_at, status, alarm_version, repair_version, restoration_version, detail)
                   VALUES (?,?,?,?,?,?)""",
                (
                    utc_now_str(), "success",
                    versions["alarms"], versions["repairs"], versions["restorations"],
                    json.dumps({"unapplied_receipts": unapplied}, ensure_ascii=False),
                ),
            )
            pid = cur.lastrowid
            for ev in events:
                ecur = conn.execute(
                    """INSERT INTO events
                       (publication_id, feeder_no, start_time, end_time,
                        closed, closed_by_serial, unclosed_reason)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        pid, ev["feeder_no"], to_store(ev["start"]), to_store(ev["end"]),
                        1 if ev["closed"] else 0, ev["closed_by"], ev["unclosed_reason"],
                    ),
                )
                eid = ecur.lastrowid
                for device_no in ev["devices"]:
                    conn.execute(
                        "INSERT INTO event_devices (event_id, device_no) VALUES (?,?)",
                        (eid, device_no),
                    )
                for alarm in ev["alarms"]:
                    conn.execute(
                        "INSERT INTO event_alarms (event_id, alarm_id) VALUES (?,?)",
                        (eid, alarm["id"]),
                    )
            return {
                "publication_id": pid,
                "status": "success",
                "versions": {
                    "alarm": versions["alarms"],
                    "repair": versions["repairs"],
                    "restoration": versions["restorations"],
                },
                "event_count": len(events),
                "unapplied_receipts": unapplied,
            }
    except Exception as exc:
        _record_failed_publication(exc)
        raise


def _record_failed_publication(exc: Exception) -> None:
    """发布失败留痕；留痕本身失败不再向外抛。"""
    try:
        with get_conn() as conn:
            try:
                versions = _input_versions(conn)
            except Exception:
                versions = {t: 0 for t in INPUT_TABLES}
            conn.execute(
                """INSERT INTO publications
                   (created_at, status, alarm_version, repair_version, restoration_version,
                    error, detail)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    utc_now_str(), "failed",
                    versions["alarms"], versions["repairs"], versions["restorations"],
                    f"{type(exc).__name__}: {exc}", "{}",
                ),
            )
    except Exception:
        pass


# ---------------------------------------------------------------- 查询

def _pub_dict(row) -> dict[str, Any]:
    return {
        "publication_id": row["id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "versions": {
            "alarm": row["alarm_version"],
            "repair": row["repair_version"],
            "restoration": row["restoration_version"],
        },
        "error": row["error"],
        "detail": json.loads(row["detail"] or "{}"),
    }


def _event_dict(conn, row, with_sources: bool = True) -> dict[str, Any]:
    eid = row["id"]
    out: dict[str, Any] = {
        "event_id": eid,
        "publication_id": row["publication_id"],
        "feeder_no": row["feeder_no"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "closed": bool(row["closed"]),
        "closed_by_serial": row["closed_by_serial"],
        "unclosed_reason": row["unclosed_reason"],
        "devices": [
            r["device_no"]
            for r in conn.execute(
                "SELECT device_no FROM event_devices WHERE event_id=? ORDER BY device_no", (eid,)
            )
        ],
    }
    if with_sources:
        out["alarms"] = [
            dict(r)
            for r in conn.execute(
                """SELECT a.* FROM alarms a
                   JOIN event_alarms ea ON ea.alarm_id = a.id
                   WHERE ea.event_id=? ORDER BY a.start_time, a.id""",
                (eid,),
            )
        ]
        # 与事件时间窗相交的抢修单，仅作关联展示
        out["repairs"] = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM repairs
                   WHERE feeder_no=? AND start_time<=? AND end_time>=?
                   ORDER BY start_time, id""",
                (row["feeder_no"], row["end_time"], row["start_time"]),
            )
        ]
    return out


def list_publications(limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [
            _pub_dict(r)
            for r in conn.execute("SELECT * FROM publications ORDER BY id DESC LIMIT ?", (limit,))
        ]


def publication_detail(publication_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM publications WHERE id=?", (publication_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"发布 {publication_id} 不存在")
        events = []
        if row["status"] == "success":
            events = [
                _event_dict(conn, r)
                for r in conn.execute(
                    "SELECT * FROM events WHERE publication_id=? "
                    "ORDER BY feeder_no, start_time, id",
                    (publication_id,),
                )
            ]
        return {"publication": _pub_dict(row), "events": events}


def list_events(publication_id: Optional[int], feeder_no: Optional[str]) -> dict[str, Any]:
    """查事件。publication_id 缺省时取最新成功发布（失败发布自动回落上一版）。"""
    with get_conn() as conn:
        if publication_id is None:
            row = conn.execute(
                "SELECT * FROM publications WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise LookupError("尚无成功发布，请先调用 POST /api/publications")
        else:
            row = conn.execute(
                "SELECT * FROM publications WHERE id=?", (publication_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"发布 {publication_id} 不存在")
            if row["status"] != "success":
                raise LookupError(f"发布 {publication_id} 是失败发布，无事件数据")
        sql = "SELECT * FROM events WHERE publication_id=?"
        params: list[Any] = [row["id"]]
        if feeder_no:
            sql += " AND feeder_no=?"
            params.append(feeder_no)
        sql += " ORDER BY feeder_no, start_time, id"
        events = [_event_dict(conn, r) for r in conn.execute(sql, params)]
        return {"publication": _pub_dict(row), "events": events}


def get_event(event_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return None
        return _event_dict(conn, row)


# ================================================================ 计划公告

class PlanNoticeConflict(Exception):
    """计划公告冲突：同计划同公告时刻的异文，或同一计划馈线不一致。"""


def _notice_dict(row) -> dict[str, Any]:
    return {
        "notice_id": row["id"],
        "plan_no": row["plan_no"],
        "feeder_no": row["feeder_no"],
        "announced_at": row["announced_at"],
        "planned_start": row["planned_start"],
        "planned_end": row["planned_end"],
        "kind": row["kind"],
        "received_at": row["received_at"],
    }


def register_plan_notice(*, plan_no: str, feeder_no: str, announced_at: datetime,
                         planned_start: Optional[datetime], planned_end: Optional[datetime],
                         kind: str) -> dict[str, Any]:
    """登记一条计划公告/撤销。

    - (plan_no, announced_at) 为幂等键：原样重送返回 created=False，不重复入库；
      同键异文抛 PlanNoticeConflict（拒收，不入库）；
    - 同一计划固定馈线：与已登记馈线不一致抛 PlanNoticeConflict；
    - 新公告入库后，核算结果会因此改变的供数中日档在同一事务内停供（stale）。
    """
    if kind not in ("notice", "cancel"):
        raise ValueError("kind 只能是 notice 或 cancel")
    if kind == "notice" and (planned_start is None or planned_end is None):
        raise ValueError("kind=notice 时 planned_start / planned_end 必填")
    if planned_start is not None and planned_end is not None and planned_end < planned_start:
        raise ValueError("planned_end 不能早于 planned_start")

    announced_s = to_store(announced_at)
    ps_s = to_store(planned_start) if planned_start is not None else None
    pe_s = to_store(planned_end) if planned_end is not None else None

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM plan_notices WHERE plan_no=? AND announced_at=?",
            (plan_no, announced_s),
        ).fetchone()
        if row is not None:
            consistent = (
                row["feeder_no"] == feeder_no and row["kind"] == kind
                and row["planned_start"] == ps_s and row["planned_end"] == pe_s
            )
            if not consistent:
                raise PlanNoticeConflict(
                    f"计划 {plan_no} 在 {announced_s} 已登记异文公告，拒收")
            return {"created": False, "consistent": True,
                    "record": _notice_dict(row), "staled_archives": []}

        other = conn.execute(
            "SELECT feeder_no FROM plan_notices WHERE plan_no=? AND feeder_no<>? LIMIT 1",
            (plan_no, feeder_no),
        ).fetchone()
        if other is not None:
            raise PlanNoticeConflict(
                f"计划 {plan_no} 已登记馈线 {other['feeder_no']}，同一计划固定馈线，拒收")

        cur = conn.execute(
            """INSERT INTO plan_notices
               (plan_no, feeder_no, announced_at, planned_start, planned_end, kind, received_at)
               VALUES (?,?,?,?,?,?,?)""",
            (plan_no, feeder_no, announced_s, ps_s, pe_s, kind, utc_now_str()),
        )
        notice = conn.execute(
            "SELECT * FROM plan_notices WHERE id=?", (cur.lastrowid,)).fetchone()
        staled = _stale_affected_archives(conn, notice)
        return {"created": True, "consistent": True,
                "record": _notice_dict(notice), "staled_archives": staled}


def list_plan_notices(plan_no: Optional[str] = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if plan_no:
            rows = conn.execute(
                "SELECT * FROM plan_notices WHERE plan_no=? ORDER BY announced_at, id",
                (plan_no,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM plan_notices ORDER BY plan_no, announced_at, id"
            ).fetchall()
        return [_notice_dict(r) for r in rows]


def _stale_affected_archives(conn, notice) -> list[dict[str, Any]]:
    """新公告入库后，把分段或依据会因此改变的供数中日档标记为停供（stale）。

    判定：对日档覆盖的某个已闭合事件，若该公告时刻不晚于事件开始，且同计划在
    该日档依据的公告版本（id <= notice_version）内没有更晚于它且不晚于事件开始
    的公告，则它会成为该计划新的末条通知，分段或依据必然改变。
    """
    staled: list[dict[str, Any]] = []
    serving = conn.execute(
        "SELECT * FROM daily_archives WHERE feeder_no=? AND status='serving'",
        (notice["feeder_no"],),
    ).fetchall()
    for arch in serving:
        events = _closed_events_of_day(
            conn, arch["publication_id"], arch["feeder_no"], arch["date"])
        for ev in events:
            if notice["announced_at"] > ev["start_time"]:
                continue  # 公告晚于停电开始，对该事件不生效
            later = conn.execute(
                """SELECT 1 FROM plan_notices
                   WHERE plan_no=? AND id<=? AND announced_at>? AND announced_at<=?
                   LIMIT 1""",
                (notice["plan_no"], arch["notice_version"],
                 notice["announced_at"], ev["start_time"]),
            ).fetchone()
            if later is None:
                conn.execute(
                    "UPDATE daily_archives SET status='stale' WHERE id=?", (arch["id"],))
                staled.append({
                    "archive_id": arch["id"],
                    "publication_id": arch["publication_id"],
                    "feeder_no": arch["feeder_no"],
                    "date": arch["date"],
                })
                break
    return staled


# ================================================================ 日档核算

def _date_str(day: Any) -> str:
    """归一化 UTC 日期为 YYYY-MM-DD；非法输入抛 ValueError。"""
    if isinstance(day, datetime):
        raise ValueError("date 必须是 UTC 日期（YYYY-MM-DD），不能带时刻")
    if isinstance(day, date_cls):
        return day.isoformat()
    text = str(day)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"非法 UTC 日期：{text!r}，应为 YYYY-MM-DD") from None
    return text


def _day_bounds(date_str: str) -> tuple[datetime, datetime]:
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return day, day + timedelta(days=1)


def _closed_events_of_day(conn, publication_id: int, feeder_no: str, date_str: str):
    """该发布号+馈线下，与当天（UTC）有交集的已闭合事件。"""
    day_start, day_end = _day_bounds(date_str)
    return conn.execute(
        """SELECT * FROM events
           WHERE publication_id=? AND feeder_no=? AND closed=1
             AND start_time < ? AND end_time > ?
           ORDER BY start_time, id""",
        (publication_id, feeder_no, to_store(day_end), to_store(day_start)),
    ).fetchall()


def _last_covering_notices(notices_by_plan: dict[str, list[dict]],
                           ev_start_s: str) -> list[dict]:
    """各计划在停电开始时已公告的最后一条通知；末条为撤销的计划不计。"""
    covering: list[dict] = []
    for plan_no in sorted(notices_by_plan):
        last: Optional[dict] = None
        for n in notices_by_plan[plan_no]:  # 已按 announced_at, id 升序
            if n["announced_at"] <= ev_start_s:
                last = n
            else:
                break
        if last is None or last["kind"] == "cancel":
            continue
        if last["planned_start"] is None or last["planned_end"] is None:
            continue
        covering.append(last)
    return covering


def _split_portion(event_id: int, p_start: datetime, p_end: datetime,
                   covering: list[tuple[datetime, datetime, dict]]) -> list[dict]:
    """把事件落在当天的部分 [p_start, p_end) 切成计划内/计划外段。

    covering 为已与当天部分求交后的计划覆盖区间；重叠覆盖只计一次；
    相邻且类型、依据完全相同的段合并。
    """
    points = {p_start, p_end}
    for s, e, _ in covering:
        points.add(s)
        points.add(e)
    pts = sorted(points)
    segments: list[dict] = []
    for a, b in zip(pts, pts[1:]):
        if a >= b:
            continue
        basis = [n for (s, e, n) in covering if s <= a and b <= e]
        kind = "planned" if basis else "unplanned"
        key = (kind, tuple(sorted(n["id"] for n in basis)))
        if segments and segments[-1]["_key"] == key:
            segments[-1]["seg_end"] = b
        else:
            segments.append({"_key": key, "event_id": event_id, "seg_start": a,
                             "seg_end": b, "kind": kind, "basis": basis})
    return segments


def _compute_daily_segments(conn, publication_id: int, feeder_no: str, date_str: str,
                            notice_version: int) -> tuple[list[dict], list[int]]:
    """核算核心：逐已闭合事件，把落在当天的部分分段。返回 (分段列表, 事件id列表)。"""
    day_start, day_end = _day_bounds(date_str)
    events = _closed_events_of_day(conn, publication_id, feeder_no, date_str)
    notices_by_plan: dict[str, list[dict]] = {}
    for r in conn.execute(
            """SELECT * FROM plan_notices
               WHERE feeder_no=? AND id<=? ORDER BY plan_no, announced_at, id""",
            (feeder_no, notice_version)):
        notices_by_plan.setdefault(r["plan_no"], []).append(dict(r))

    segments: list[dict] = []
    event_ids: list[int] = []
    for ev in events:
        ev_start, ev_end = from_store(ev["start_time"]), from_store(ev["end_time"])
        p_start, p_end = max(ev_start, day_start), min(ev_end, day_end)
        if p_start >= p_end:
            continue
        event_ids.append(ev["id"])
        covering: list[tuple[datetime, datetime, dict]] = []
        for n in _last_covering_notices(notices_by_plan, ev["start_time"]):
            s = max(from_store(n["planned_start"]), p_start)
            e = min(from_store(n["planned_end"]), p_end)
            if s < e:
                covering.append((s, e, n))
        segments.extend(_split_portion(ev["id"], p_start, p_end, covering))
    return segments, event_ids


def _basis_dict(n: dict) -> dict[str, Any]:
    return {
        "notice_id": n["id"],
        "plan_no": n["plan_no"],
        "announced_at": n["announced_at"],
        "planned_start": n["planned_start"],
        "planned_end": n["planned_end"],
    }


def _write_daily_archive(conn, *, publication_id: int, feeder_no: str, date_str: str,
                         notice_version: int, segments: list[dict], event_ids: list[int],
                         planned_minutes: float, unplanned_minutes: float) -> tuple[int, Optional[dict]]:
    """写入新日档（serving）并把原供数中日档改为 superseded。返回 (新日档号, 原供数中日档)。"""
    prev = conn.execute(
        """SELECT * FROM daily_archives
           WHERE publication_id=? AND feeder_no=? AND date=? AND status='serving'""",
        (publication_id, feeder_no, date_str),
    ).fetchone()
    if prev is not None:
        conn.execute(
            "UPDATE daily_archives SET status='superseded' WHERE id=?", (prev["id"],))
    cur = conn.execute(
        """INSERT INTO daily_archives
           (created_at, publication_id, feeder_no, date, notice_version, status,
            event_count, planned_minutes, unplanned_minutes)
           VALUES (?,?,?,?,?,'serving',?,?,?)""",
        (utc_now_str(), publication_id, feeder_no, date_str, notice_version,
         len(event_ids), round(planned_minutes, 6), round(unplanned_minutes, 6)),
    )
    archive_id = cur.lastrowid
    for s in segments:
        conn.execute(
            """INSERT INTO daily_archive_segments
               (archive_id, event_id, seg_start, seg_end, kind, basis)
               VALUES (?,?,?,?,?,?)""",
            (archive_id, s["event_id"], to_store(s["seg_start"]), to_store(s["seg_end"]),
             s["kind"], json.dumps([_basis_dict(n) for n in s["basis"]], ensure_ascii=False)),
        )
    return archive_id, prev


def account_daily_archive(publication_id: int, feeder_no: str, day: Any) -> dict[str, Any]:
    """核算/重算日档：依据生效时已入库的公告（notice_version 固定）分段并整体落库。

    校验、计算、写入在同一事务内：写入失败整体回滚，保留此前供数状态，
    并另记一条 failed 日档留痕；被停供的日档只有重算成功才恢复供数。
    """
    date_str = _date_str(day)
    try:
        with get_conn() as conn:
            pub = conn.execute(
                "SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
            if pub is None:
                raise LookupError(f"发布 {publication_id} 不存在")
            if pub["status"] != "success":
                raise ValueError(f"发布 {publication_id} 是失败发布，不能核算日档")

            notice_version = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS v FROM plan_notices").fetchone()["v"]
            segments, event_ids = _compute_daily_segments(
                conn, publication_id, feeder_no, date_str, notice_version)
            planned = sum((s["seg_end"] - s["seg_start"]).total_seconds()
                          for s in segments if s["kind"] == "planned") / 60
            unplanned = sum((s["seg_end"] - s["seg_start"]).total_seconds()
                            for s in segments if s["kind"] == "unplanned") / 60

            had_stale = conn.execute(
                """SELECT 1 FROM daily_archives
                   WHERE publication_id=? AND feeder_no=? AND date=? AND status='stale'
                   LIMIT 1""",
                (publication_id, feeder_no, date_str),
            ).fetchone()
            archive_id, prev = _write_daily_archive(
                conn, publication_id=publication_id, feeder_no=feeder_no, date_str=date_str,
                notice_version=notice_version, segments=segments, event_ids=event_ids,
                planned_minutes=planned, unplanned_minutes=unplanned)
            previous_status = "serving" if prev is not None else ("stale" if had_stale else "none")

            row = conn.execute(
                "SELECT * FROM daily_archives WHERE id=?", (archive_id,)).fetchone()
            out = _archive_dict(conn, row)
            out["previous_status"] = previous_status
            out["superseded_archive_id"] = prev["id"] if prev is not None else None
            return out
    except (LookupError, ValueError):
        raise
    except Exception as exc:
        _record_failed_archive(publication_id, feeder_no, date_str, exc)
        raise


def _record_failed_archive(publication_id: int, feeder_no: str, date_str: str,
                           exc: Exception) -> None:
    """核算失败留痕（审计用）；留痕本身失败不再向外抛。供数状态保持不变。"""
    try:
        with get_conn() as conn:
            try:
                v = conn.execute(
                    "SELECT COALESCE(MAX(id),0) AS v FROM plan_notices").fetchone()["v"]
            except Exception:
                v = 0
            conn.execute(
                """INSERT INTO daily_archives
                   (created_at, publication_id, feeder_no, date, notice_version,
                    status, error)
                   VALUES (?,?,?,?,?,'failed',?)""",
                (utc_now_str(), publication_id, feeder_no, date_str, v,
                 f"{type(exc).__name__}: {exc}"),
            )
    except Exception:
        pass


# ---------------------------------------------------------------- 日档查询

def _archive_dict(conn, row, with_segments: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "archive_id": row["id"],
        "created_at": row["created_at"],
        "publication_id": row["publication_id"],
        "feeder_no": row["feeder_no"],
        "date": row["date"],
        "notice_version": row["notice_version"],
        "status": row["status"],
        "error": row["error"],
        "event_count": row["event_count"],
        "planned_minutes": row["planned_minutes"],
        "unplanned_minutes": row["unplanned_minutes"],
    }
    if with_segments:
        out["segments"] = [
            {
                "event_id": s["event_id"],
                "seg_start": s["seg_start"],
                "seg_end": s["seg_end"],
                "kind": s["kind"],
                "basis": json.loads(s["basis"] or "[]"),
            }
            for s in conn.execute(
                "SELECT * FROM daily_archive_segments WHERE archive_id=? ORDER BY id",
                (row["id"],),
            )
        ]
    return out


def current_daily_archive(publication_id: int, feeder_no: str, day: Any) -> dict[str, Any]:
    """按 发布号+馈线+UTC日期 查当前日档。

    status=serving：正常供数；stale：晚录公告已令其停供，待重算（内容仅供核查，
    月报不得采用）；none：尚未核算。
    """
    date_str = _date_str(day)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM daily_archives
               WHERE publication_id=? AND feeder_no=? AND date=? AND status='serving'""",
            (publication_id, feeder_no, date_str),
        ).fetchone()
        status, arch_row = ("serving", row) if row is not None else (None, None)
        if arch_row is None:
            arch_row = conn.execute(
                """SELECT * FROM daily_archives
                   WHERE publication_id=? AND feeder_no=? AND date=? AND status='stale'
                   ORDER BY id DESC LIMIT 1""",
                (publication_id, feeder_no, date_str),
            ).fetchone()
            status = "stale" if arch_row is not None else "none"
        return {
            "publication_id": publication_id,
            "feeder_no": feeder_no,
            "date": date_str,
            "status": status,
            "archive": _archive_dict(conn, arch_row) if arch_row is not None else None,
        }


def get_daily_archive(archive_id: int) -> Optional[dict[str, Any]]:
    """按日档号回查：停供/被取代/失败的旧档均可查。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM daily_archives WHERE id=?", (archive_id,)).fetchone()
        if row is None:
            return None
        return _archive_dict(conn, row)


def list_daily_archives(publication_id: Optional[int] = None,
                        feeder_no: Optional[str] = None,
                        day: Any = None,
                        status: Optional[str] = None,
                        limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM daily_archives WHERE 1=1"
    params: list[Any] = []
    if publication_id is not None:
        sql += " AND publication_id=?"
        params.append(publication_id)
    if feeder_no:
        sql += " AND feeder_no=?"
        params.append(feeder_no)
    if day is not None:
        sql += " AND date=?"
        params.append(_date_str(day))
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [_archive_dict(conn, r) for r in conn.execute(sql, params)]
