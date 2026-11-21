"""入参模型（Pydantic v2）。所有时间必须携带时区。"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def _require_tz(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("时间必须携带时区，例如 2026-11-21T10:00:00+08:00")
    return v


class _TimedRecord(BaseModel):
    feeder_no: str = Field(min_length=1, max_length=64, description="馈线号", examples=["F01"])
    device_no: str = Field(min_length=1, max_length=64, description="设备号", examples=["D001"])
    start_time: datetime = Field(description="开始时间（带时区）", examples=["2026-11-21T10:00:00+08:00"])
    end_time: datetime = Field(description="结束时间（带时区）", examples=["2026-11-21T10:20:00+08:00"])
    serial_no: str = Field(min_length=1, max_length=128, description="来源流水号（幂等键，重发不重复入库）",
                           examples=["ALM-20261121-0001"])

    @field_validator("start_time", "end_time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _require_tz(v)

    @model_validator(mode="after")
    def _order(self):
        if self.end_time < self.start_time:
            raise ValueError("end_time 不能早于 start_time")
        return self


class AlarmIn(_TimedRecord):
    """告警：某设备在某馈线上的告警起止区间。"""


class RepairIn(_TimedRecord):
    """抢修：抢修作业起止区间（仅记录与关联展示，不参与事件开合）。"""


class RestorationIn(BaseModel):
    """复电回执：restore_time 为复电时刻，是闭合事件的有效判据。"""

    feeder_no: str = Field(min_length=1, max_length=64, description="馈线号", examples=["F01"])
    device_no: str = Field(min_length=1, max_length=64, description="设备号", examples=["D001"])
    restore_time: datetime = Field(description="复电时刻（带时区）", examples=["2026-11-21T10:22:00+08:00"])
    end_time: Optional[datetime] = Field(default=None, description="复电操作结束时间，缺省同复电时刻")
    serial_no: str = Field(min_length=1, max_length=128, description="来源流水号（幂等键）",
                           examples=["RST-20261121-0001"])

    @field_validator("restore_time", "end_time")
    @classmethod
    def _tz(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        return _require_tz(v)

    @model_validator(mode="after")
    def _order(self):
        if self.end_time is not None and self.end_time < self.restore_time:
            raise ValueError("end_time 不能早于 restore_time")
        return self


class PlanNoticeIn(BaseModel):
    """计划公告：按计划号登记馈线、实际公告时刻与计划起止；kind=cancel 表示撤销。

    幂等键为 (plan_no, announced_at)：原样重送只记一次，异文拒收（409）。
    同一计划的所有公告必须使用同一馈线号。
    """

    plan_no: str = Field(min_length=1, max_length=64, description="计划号",
                         examples=["PLAN-20261121-001"])
    feeder_no: str = Field(min_length=1, max_length=64, description="馈线号（同一计划固定）",
                           examples=["F01"])
    announced_at: datetime = Field(description="实际公告时刻（带时区）",
                                   examples=["2026-11-20T18:00:00+08:00"])
    kind: Literal["notice", "cancel"] = Field(
        default="notice", description="notice=计划公告；cancel=撤销该计划")
    planned_start: Optional[datetime] = Field(
        default=None, description="计划停电开始（kind=notice 必填）",
        examples=["2026-11-21T09:30:00+08:00"])
    planned_end: Optional[datetime] = Field(
        default=None, description="计划停电结束（kind=notice 必填）",
        examples=["2026-11-21T10:30:00+08:00"])

    @field_validator("announced_at", "planned_start", "planned_end")
    @classmethod
    def _tz(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        return _require_tz(v)

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "notice" and (self.planned_start is None or self.planned_end is None):
            raise ValueError("kind=notice 时 planned_start / planned_end 必填")
        if (self.planned_start is not None and self.planned_end is not None
                and self.planned_end < self.planned_start):
            raise ValueError("planned_end 不能早于 planned_start")
        return self


class DailyArchiveRequest(BaseModel):
    """日档核算/重算请求：按成功发布号、馈线和 UTC 日期核算已闭合事件。"""

    publication_id: int = Field(ge=1, description="成功发布号", examples=[1])
    feeder_no: str = Field(min_length=1, max_length=64, description="馈线号", examples=["F01"])
    date: date_type = Field(description="UTC 日期（YYYY-MM-DD）", examples=["2026-11-21"])
