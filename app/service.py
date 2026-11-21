"""核心业务：输入入库（幂等）、事件归并/闭合计算、版本化发布。

规则：
- 同一馈线的告警区间相交，或间隔不超过 MERGE_GAP_MINUTES（默认 5）分钟，归并为同一事件；
- 有效复电回执（复电时刻 >= 事件开始时刻）闭合事件，事件结束时间取复电时刻；
- 回执之后的告警归入后续事件；
- 无有效回执的事件保持未闭合，并给出未闭合原因；
- 每次发布固定三类输入（告警/抢修/复电回执）的版本；发布失败时记录 failed 发布，
  查询继续回落到上一成功发布；旧发布号永远可查更正前的结果。
"""
from __future__ import annotations

import json
import os
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
