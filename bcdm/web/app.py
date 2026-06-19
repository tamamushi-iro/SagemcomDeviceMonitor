"""FastAPI application: live dashboard + JSON API for BellDeviceMonitor.

The lifespan wires the engine together (optional dev route -> background poller
-> SQLite) and tears it down cleanly. Every route is behind HTTP Basic auth;
static assets are public. The dashboard polls the JSON API for live updates.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..alerts import Alerter
from ..config import Settings, get_settings
from ..db import Database
from ..dev_route import DevRoute
from ..poller import Poller
from ..router_client import RouterClient

log = logging.getLogger("bcdm")

HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
HISTORY_RANGES = ("1h", "24h", "30d", "1y")
HEATMAP_DAYS = (7, 14, 30)
SCOPES = ("watched", "active", "all")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    db = Database(settings.db_file)
    poller = Poller(settings, db, RouterClient(settings), Alerter(settings))

    app.state.settings = settings
    app.state.db = db
    app.state.poller = poller

    dev_route = DevRoute(settings)
    dev_route.__enter__()
    poller.start()
    log.info(
        "BellDeviceMonitor up: polling %s every %ss (db=%s)",
        settings.router_host, settings.poll_interval_seconds, settings.db_file,
    )
    try:
        yield
    finally:
        await poller.stop()
        dev_route.__exit__(None, None, None)
        db.close()


app = FastAPI(title="BellDeviceMonitor", lifespan=lifespan)

# -- auth --------------------------------------------------------------------

_security = HTTPBasic()


def require_auth(
    request: Request, credentials: HTTPBasicCredentials = Depends(_security)
) -> str:
    settings: Settings = request.app.state.settings
    ok_user = secrets.compare_digest(credentials.username, settings.auth_user)
    ok_pass = secrets.compare_digest(credentials.password, settings.auth_password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def db_of(request: Request) -> Database:
    return request.app.state.db


def poller_of(request: Request) -> Poller:
    return request.app.state.poller


router = APIRouter(dependencies=[Depends(require_auth)])


class DeviceUpdate(BaseModel):
    label: str | None = None
    watched: bool | None = None
    hidden: bool | None = None


# -- pages -------------------------------------------------------------------


def _page(request: Request, template: str, **ctx) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    context = {
        "host": settings.router_host,
        "poll_interval": settings.poll_interval_seconds,
        **ctx,
    }
    # Starlette's current API: TemplateResponse(request, name, context).
    return TEMPLATES.TemplateResponse(request, template, context)


@router.get("/", response_class=HTMLResponse)
async def page_now(request: Request) -> HTMLResponse:
    return _page(request, "now.html", active="now")


@router.get("/history", response_class=HTMLResponse)
async def page_history(request: Request) -> HTMLResponse:
    return _page(request, "history.html", active="history")


@router.get("/devices", response_class=HTMLResponse)
async def page_devices(request: Request) -> HTMLResponse:
    return _page(request, "devices.html", active="devices")


# -- JSON API ----------------------------------------------------------------


@router.get("/api/now")
async def api_now(request: Request) -> dict:
    return dataclasses.asdict(poller_of(request).snapshot)


@router.get("/api/history")
async def api_history(
    request: Request, range: str = Query("24h")
) -> dict:
    if range not in HISTORY_RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {HISTORY_RANGES}")
    points = await db_of(request).get_count_history(range)
    return {"range": range, "points": points}


@router.get("/api/timeline")
async def api_timeline(
    request: Request,
    range: str = Query("24h"),
    scope: str = Query("watched"),
) -> dict:
    if range not in HISTORY_RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {HISTORY_RANGES}")
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"scope must be one of {SCOPES}")
    return await db_of(request).get_presence_timeline(range, scope)


@router.get("/api/heatmap")
async def api_heatmap(
    request: Request,
    days: int = Query(14),
    scope: str = Query("all"),
) -> dict:
    if days not in HEATMAP_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {HEATMAP_DAYS}")
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"scope must be one of {SCOPES}")
    return await db_of(request).get_presence_heatmap(days, scope)


@router.get("/api/daily")
async def api_daily(request: Request, days: int = Query(14)) -> dict:
    if days not in HEATMAP_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {HEATMAP_DAYS}")
    return await db_of(request).get_daily_activity(days)


@router.get("/api/interfaces")
async def api_interfaces(request: Request, range: str = Query("24h")) -> dict:
    if range not in HISTORY_RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {HISTORY_RANGES}")
    return await db_of(request).get_interface_history(range)


@router.get("/api/devices")
async def api_devices(request: Request) -> list[dict]:
    return await db_of(request).list_devices()


@router.post("/api/devices/{mac}")
async def api_update_device(request: Request, mac: str, body: DeviceUpdate) -> dict:
    fields = body.model_dump(exclude_unset=True)
    device = await db_of(request).update_device(mac.upper(), **fields)
    if device is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    return device


@router.get("/api/devices/{mac}/events")
async def api_device_events(
    request: Request, mac: str, limit: int = Query(100, ge=1, le=1000)
) -> list[dict]:
    return await db_of(request).get_device_events(mac.upper(), limit)


@router.get("/api/events")
async def api_events(request: Request, limit: int = Query(25, ge=1, le=200)) -> list[dict]:
    return await db_of(request).get_recent_events(limit)


app.include_router(router)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
