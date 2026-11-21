"""SQLite 连接与建表。

所有输入记录（告警/抢修/复电回执）都带：馈线号、设备号、起止时间、来源流水号。
发布（publication）固定三类输入的版本；事件（event）挂在某次发布下，
旧发布及其事件永久保留，供迟到更正前结果的回溯查询。

计划公告（plan_notice）按计划号登记，(plan_no, announced_at) 为幂等键；
日档（daily_archive）是某成功发布号 + 馈线 + UTC 日期下已闭合事件的
计划内/计划外分段核算结果，每次核算产生新日档号，旧日档永久保留可回查。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DEFAULT_DB_PATH = "./outage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feeder_no   TEXT NOT NULL,
    device_no   TEXT NOT NULL,
    start_time  TEXT NOT NULL,          -- UTC ISO8601 定宽字符串
    end_time    TEXT NOT NULL,
    serial_no   TEXT NOT NULL UNIQUE,   -- 来源流水号（幂等键）
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repairs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feeder_no   TEXT NOT NULL,
    device_no   TEXT NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    serial_no   TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS restorations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feeder_no   TEXT NOT NULL,
    device_no   TEXT NOT NULL,
    start_time  TEXT NOT NULL,          -- 复电时刻
    end_time    TEXT NOT NULL,          -- 复电操作结束时刻（缺省同复电时刻）
    serial_no   TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL,              -- success / failed
    alarm_version       INTEGER NOT NULL,           -- 固定的三类输入版本
    repair_version      INTEGER NOT NULL,
    restoration_version INTEGER NOT NULL,
    error               TEXT,                       -- 失败原因
    detail              TEXT NOT NULL DEFAULT '{}'  -- JSON：未生效回执等
);
CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id   INTEGER NOT NULL REFERENCES publications(id),
    feeder_no        TEXT NOT NULL,
    start_time       TEXT NOT NULL,
    end_time         TEXT NOT NULL,   -- 已闭合=复电时刻；未闭合=最后一条告警的结束时间
    closed           INTEGER NOT NULL,
    closed_by_serial TEXT,            -- 闭合事件的回执流水号
    unclosed_reason  TEXT             -- 未闭合原因
);
CREATE TABLE IF NOT EXISTS event_devices (
    event_id  INTEGER NOT NULL REFERENCES events(id),
    device_no TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_alarms (
    event_id INTEGER NOT NULL REFERENCES events(id),
    alarm_id INTEGER NOT NULL REFERENCES alarms(id)
);
CREATE TABLE IF NOT EXISTS plan_notices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_no       TEXT NOT NULL,            -- 计划号（同一计划固定馈线）
    feeder_no     TEXT NOT NULL,
    announced_at  TEXT NOT NULL,            -- 实际公告时刻 UTC
    planned_start TEXT,                     -- 计划停电开始（撤销可为空）
    planned_end   TEXT,                     -- 计划停电结束（撤销可为空）
    kind          TEXT NOT NULL,            -- notice / cancel
    received_at   TEXT NOT NULL,            -- 入库时刻
    UNIQUE (plan_no, announced_at)          -- 幂等键：同计划同公告时刻只记一次
);
CREATE TABLE IF NOT EXISTS daily_archives (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,  -- 日档号
    created_at        TEXT NOT NULL,
    publication_id    INTEGER NOT NULL REFERENCES publications(id),
    feeder_no         TEXT NOT NULL,
    date              TEXT NOT NULL,        -- UTC 日期 YYYY-MM-DD
    notice_version    INTEGER NOT NULL,     -- 核算依据的公告版本（生效时已入库的最大 id）
    status            TEXT NOT NULL,        -- serving / stale / superseded / failed
    error             TEXT,                 -- 核算失败原因（status=failed 时）
    event_count       INTEGER NOT NULL DEFAULT 0,
    planned_minutes   REAL NOT NULL DEFAULT 0,
    unplanned_minutes REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_archive_segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL REFERENCES daily_archives(id),
    event_id   INTEGER NOT NULL REFERENCES events(id),
    seg_start  TEXT NOT NULL,
    seg_end    TEXT NOT NULL,
    kind       TEXT NOT NULL,             -- planned / unplanned
    basis      TEXT NOT NULL DEFAULT '[]' -- JSON：计划内段的通知依据
);
CREATE INDEX IF NOT EXISTS idx_alarms_feeder       ON alarms(feeder_no, start_time);
CREATE INDEX IF NOT EXISTS idx_restorations_feeder ON restorations(feeder_no, start_time);
CREATE INDEX IF NOT EXISTS idx_repairs_feeder      ON repairs(feeder_no, start_time);
CREATE INDEX IF NOT EXISTS idx_events_pub          ON events(publication_id);
CREATE INDEX IF NOT EXISTS idx_plan_notices_feeder ON plan_notices(feeder_no, id);
CREATE INDEX IF NOT EXISTS idx_daily_archives_key  ON daily_archives(publication_id, feeder_no, date, status);
CREATE INDEX IF NOT EXISTS idx_segments_archive    ON daily_archive_segments(archive_id);
"""


def get_db_path() -> str:
    return os.environ.get("OUTAGE_DB_PATH", DEFAULT_DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    path = os.path.abspath(get_db_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """事务性连接：正常退出提交，异常回滚。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
