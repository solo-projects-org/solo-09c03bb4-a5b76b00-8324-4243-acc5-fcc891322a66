"""入参模型（Pydantic v2）。所有时间必须携带时区。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

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
