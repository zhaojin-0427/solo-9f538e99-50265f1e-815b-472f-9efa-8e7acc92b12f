"""采集与查询 API。"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import db as dbmod
from . import service

HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")


def _check_hour(hour: str) -> str:
    hour = hour.strip()
    if not HOUR_RE.match(hour):
        raise HTTPException(400, "hour must be a UTC hour like '2026-11-18T10'")
    try:
        datetime.strptime(hour, "%Y-%m-%dT%H")
    except ValueError:
        raise HTTPException(400, f"invalid UTC hour: {hour}")
    return hour


@asynccontextmanager
async def lifespan(app: FastAPI):
    dbmod.init()
    yield


app = FastAPI(title="PM2.5 Revision Collector", lifespan=lifespan)


class CollectRequest(BaseModel):
    hour: str


@app.get("/")
def index():
    return {"service": "pm25-collector", "docs": "/docs"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/collections")
def create_collection(req: CollectRequest):
    """采集某 UTC 小时:先固定源站代际与目标版,再逐版补齐(新代际从首版全量重建)。"""
    hour = _check_hour(req.hour)
    try:
        return service.collect_hour(hour)
    except service.HourBusy:
        raise HTTPException(409, f"collection already running for hour {hour}")


@app.get("/collections/{hour}")
def collection_status(hour: str):
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        return dict(row)
    finally:
        db.close()


@app.get("/hours")
def list_hours():
    db = dbmod.connect()
    try:
        rows = db.execute("SELECT * FROM hours ORDER BY hour").fetchall()
        return {"hours": [dict(r) for r in rows]}
    finally:
        db.close()


@app.get("/hours/{hour}/versions")
def list_versions(hour: str):
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        vers = db.execute(
            "SELECT * FROM versions WHERE hour=? ORDER BY confirmed_at, generation, version",
            (hour,),
        ).fetchall()
        return {
            "hour": hour,
            "current_generation": row["current_generation"],
            "current_version": row["current_version"],
            "versions": [dict(v) for v in vers],
        }
    finally:
        db.close()


@app.get("/hours/{hour}/data")
def hour_data(
    hour: str,
    generation: Optional[str] = Query(default=None, description="缺省取当前代际"),
    version: Optional[int] = Query(default=None, description="缺省取当前版;"
                                     "指定 generation 而未指定 version 时取该代际最新确认版"),
    station_id: Optional[str] = Query(default=None),
):
    """查询某代际某已确认版当时的完整数据(默认:当前代际当前版)。"""
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        gen = generation if generation is not None else row["current_generation"]
        if gen is None:
            raise HTTPException(404, f"hour {hour} has no confirmed version yet")
        if version is not None:
            v = version
        elif generation is None:
            v = row["current_version"]
        else:
            # 只指定代际:取该代际最新确认版
            latest = db.execute(
                "SELECT MAX(version) AS v FROM versions WHERE hour=? AND generation=?",
                (hour, gen),
            ).fetchone()["v"]
            if latest is None:
                raise HTTPException(404, f"generation {gen} of hour {hour} is not confirmed")
            v = latest
        if v == 0:
            raise HTTPException(404, f"hour {hour} has no confirmed version yet")
        vrow = db.execute(
            "SELECT * FROM versions WHERE hour=? AND generation=? AND version=?",
            (hour, gen, v),
        ).fetchone()
        if vrow is None:
            raise HTTPException(
                404, f"version {v} of generation {gen} for hour {hour} is not confirmed"
            )
        if station_id is not None:
            rows = db.execute(
                "SELECT station_id, pm25 FROM entries "
                "WHERE hour=? AND generation=? AND version=? AND station_id=?",
                (hour, gen, v, station_id),
            ).fetchall()
            if not rows:
                raise HTTPException(404, f"station {station_id} not present in version {v}")
        else:
            rows = db.execute(
                "SELECT station_id, pm25 FROM entries "
                "WHERE hour=? AND generation=? AND version=? ORDER BY station_id",
                (hour, gen, v),
            ).fetchall()
        return {
            "hour": hour,
            "generation": gen,
            "version": v,
            "is_current": gen == row["current_generation"] and v == row["current_version"],
            "station_count": len(rows),
            "stations": [{"station_id": r["station_id"], "pm25": r["pm25"]} for r in rows],
        }
    finally:
        db.close()
