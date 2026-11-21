"""FastAPI 入口：接收告警/抢修/复电回执，发布并查询停电事件；登记计划公告，核算并查询日档。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date as date_type
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from . import service
from .db import init_db
from .schemas import AlarmIn, DailyArchiveRequest, PlanNoticeIn, RepairIn, RestorationIn


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="停电事件还原服务",
    version="1.1.0",
    description=(
        "配电调度员停电经过还原：分别接收告警、抢修、复电回执；同一馈线告警区间相交或"
        "相隔不超过 5 分钟归并为同一事件；有效复电回执闭合事件，回执之后的告警归入后续"
        "事件；每次发布固定三类输入版本，旧发布号可回溯更正前结果，发布失败自动回落上一版。"
        "计划公告按计划号登记（同计划同公告时刻原样重送只记一次，异文拒收）；"
        "日档按成功发布号+馈线+UTC日期核算已闭合事件的计划内/计划外分段，"
        "晚录公告若改变分段或依据则对应日档随入库停供，重算成功才恢复，旧日档可按号回查。"
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


# ------------------------------------------------------------ 计划公告

@app.post("/api/plan-notices", summary="登记计划公告/撤销（按计划号，幂等；异文拒收）", tags=["plan"])
def post_plan_notice(body: PlanNoticeIn):
    try:
        return service.register_plan_notice(
            plan_no=body.plan_no, feeder_no=body.feeder_no,
            announced_at=body.announced_at, planned_start=body.planned_start,
            planned_end=body.planned_end, kind=body.kind,
        )
    except service.PlanNoticeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/plan-notices", summary="计划公告列表（可按计划号过滤）", tags=["plan"])
def get_plan_notices(plan_no: Optional[str] = Query(None, description="按计划号过滤")):
    return {"notices": service.list_plan_notices(plan_no)}


# ------------------------------------------------------------ 日档

@app.post("/api/daily-archives",
          summary="核算/重算日档（按成功发布号+馈线+UTC日期，读取已闭合事件）",
          tags=["daily-archive"])
def post_daily_archive(body: DailyArchiveRequest):
    try:
        return service.account_daily_archive(body.publication_id, body.feeder_no, body.date)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # 写入失败整体回滚，此前供数状态保持不变
        raise HTTPException(
            status_code=500,
            detail=f"日档核算失败，此前供数状态保持不变。原因：{type(exc).__name__}: {exc}",
        )


@app.get("/api/daily-archives", summary="日档列表（新→旧，可按发布号/馈线/日期/状态过滤）",
         tags=["daily-archive"])
def get_daily_archives(
    publication_id: Optional[int] = Query(None),
    feeder_no: Optional[str] = Query(None),
    date: Optional[date_type] = Query(None, description="UTC 日期 YYYY-MM-DD"),
    status: Optional[str] = Query(None, description="serving / stale / superseded / failed"),
    limit: int = Query(50, ge=1, le=500),
):
    return {"archives": service.list_daily_archives(publication_id, feeder_no, date, status, limit)}


@app.get("/api/daily-archives/current",
         summary="按发布号+馈线+UTC日期查当前日档（serving=供数 / stale=停供待重算 / none=未核算）",
         tags=["daily-archive"])
def get_current_daily_archive(
    publication_id: int = Query(..., description="成功发布号"),
    feeder_no: str = Query(..., description="馈线号"),
    date: date_type = Query(..., description="UTC 日期 YYYY-MM-DD"),
):
    return service.current_daily_archive(publication_id, feeder_no, date)


@app.get("/api/daily-archives/{archive_id}",
         summary="按日档号回查（停供/被取代的旧档均可查）", tags=["daily-archive"])
def get_daily_archive(archive_id: int):
    archive = service.get_daily_archive(archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail=f"日档 {archive_id} 不存在")
    return archive
