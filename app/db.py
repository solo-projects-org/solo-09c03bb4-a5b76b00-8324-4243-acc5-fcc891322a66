"""SQLite 连接与建表。

所有输入记录（告警/抢修/复电回执）都带：馈线号、设备号、起止时间、来源流水号。
发布（publication）固定三类输入的版本；事件（event）挂在某次发布下，
旧发布及其事件永久保留，供迟到更正前结果的回溯查询。
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
CREATE INDEX IF NOT EXISTS idx_alarms_feeder       ON alarms(feeder_no, start_time);
CREATE INDEX IF NOT EXISTS idx_restorations_feeder ON restorations(feeder_no, start_time);
CREATE INDEX IF NOT EXISTS idx_repairs_feeder      ON repairs(feeder_no, start_time);
CREATE INDEX IF NOT EXISTS idx_events_pub          ON events(publication_id);
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
