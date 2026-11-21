"""FastAPI 入口：接收告警/抢修/复电回执，发布并查询停电事件。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from . import service
from .db import init_db
from .schemas import AlarmIn, RepairIn, RestorationIn


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="停电事件还原服务",
    version="1.0.0",
    description=(
        "配电调度员停电经过还原：分别接收告警、抢修、复电回执；同一馈线告警区间相交或"
        "相隔不超过 5 分钟归并为同一事件；有效复电回执闭合事件，回执之后的告警归入后续"
        "事件；每次发布固定三类输入版本，旧发布号可回溯更正前结果，发布失败自动回落上一版。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ------------------------------------------------------------ 输入接收

@app.post("/api/alarms", summary="接收告警", tags=["ingest"])
def post_alarm(body: AlarmIn):
    return service.ingest(
        "alarm",
        feeder_no=body.feeder_no, device_no=body.device_no,
        start_time=body.start_time, end_time=body.end_time,
        serial_no=body.serial_no,
    )


@app.post("/api/repairs", summary="接收抢修", tags=["ingest"])
def post_repair(body: RepairIn):
    return service.ingest(
        "repair",
        feeder_no=body.feeder_no, device_no=body.device_no,
        start_time=body.start_time, end_time=body.end_time,
        serial_no=body.serial_no,
    )


@app.post("/api/restorations", summary="接收复电回执", tags=["ingest"])
def post_restoration(body: RestorationIn):
    return service.ingest(
        "restoration",
        feeder_no=body.feeder_no, device_no=body.device_no,
        start_time=body.restore_time, end_time=body.end_time or body.restore_time,
        serial_no=body.serial_no,
    )


# ------------------------------------------------------------ 发布

@app.post("/api/publications", summary="发布：固定三类输入版本并重算事件", tags=["publish"])
def post_publication():
    try:
        return service.publish()
    except Exception as exc:  # 失败时上一成功发布继续对外提供
        raise HTTPException(
            status_code=500,
            detail=f"发布失败，查询仍回落到上一成功发布。原因：{type(exc).__name__}: {exc}",
        )


@app.get("/api/publications", summary="发布列表（新→旧）", tags=["publish"])
def get_publications(limit: int = Query(50, ge=1, le=500)):
    return {"publications": service.list_publications(limit)}


@app.get("/api/publications/{publication_id}", summary="某次发布及其事件", tags=["publish"])
def get_publication(publication_id: int):
    try:
        return service.publication_detail(publication_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ------------------------------------------------------------ 事件查询

@app.get("/api/events", summary="查询事件（默认最新成功发布）", tags=["query"])
def get_events(
    publication_id: Optional[int] = Query(None, description="缺省取最新成功发布；可填旧发布号回溯更正前结果"),
    feeder_no: Optional[str] = Query(None, description="按馈线号过滤"),
):
    try:
        return service.list_events(publication_id, feeder_no)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/events/{event_id}", summary="单个事件详情（含来源告警与关联抢修）", tags=["query"])
def get_event(event_id: int):
    event = service.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_id} 不存在")
    return event
