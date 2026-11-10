"""FastAPI application: live dashboard + JSON API for BellDeviceMonitor.

The lifespan wires the engine together (optional dev route -> background poller
-> SQLite) and tears it down cleanly. Every route is behind HTTP Basic auth;
static assets are public. The pages fetch their data from the JSON API, which
serves what :mod:`bcdm.insights` computes from the stored history.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .. import insights
from ..alerts import Alerter
from ..analytics import SAMPLE_GAP_S
from ..config import Settings, get_settings
from ..db import OWNER_MAX_LEN, Database
from ..dev_route import DevRoute
from ..poller import Poller
from ..router_client import RouterClient

log = logging.getLogger("bcdm")

HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
PATTERN_WEEKS = (4, 8, 12, 0)  # 0 = all history
MATRIX_DAYS = (30, 90, 0)
HEALTH_DAYS = (7, 30, 90)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    # A few missed polls are a hiccup; longer than that and device state is unknown.
    sample_gap = max(SAMPLE_GAP_S, int(4 * settings.poll_interval_seconds))
    db = Database(settings.db_file, sample_gap_s=sample_gap)
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
app.state.clock = time.time  # what "now" is; scripts/dev_server.py can pin it to a snapshot


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Make browsers check static files for changes, so an upgrade never runs old
    scripts against a new API (unchanged files still answer 304)."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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


def now_of(request: Request) -> int:
    return int(request.app.state.clock())


def tz_of(tz: str = Query("UTC", max_length=64)) -> ZoneInfo:
    """The viewer's timezone (IANA name), which decides where days and hours fall."""
    try:
        return ZoneInfo(tz)
    except Exception as exc:  # unknown key, malformed key, or not a zone file
        raise HTTPException(status_code=400, detail="Unknown timezone") from exc


router = APIRouter(dependencies=[Depends(require_auth)])


class DeviceUpdate(BaseModel):
    label: str | None = Field(None, max_length=80)
    owner: str | None = Field(None, max_length=OWNER_MAX_LEN)
    watched: bool | None = None
    hidden: bool | None = None
    carried: bool | None = None  # goes where its owner goes; null = judge by the hostname


class DevicesUpdate(DeviceUpdate):
    macs: list[str] = Field(min_length=1, max_length=500)


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
async def page_overview(request: Request) -> HTMLResponse:
    return _page(request, "overview.html", active="overview")


@router.get("/patterns", response_class=HTMLResponse)
async def page_patterns(request: Request) -> HTMLResponse:
    return _page(request, "patterns.html", active="patterns")


@router.get("/devices", response_class=HTMLResponse)
async def page_devices(request: Request) -> HTMLResponse:
    return _page(request, "devices.html", active="devices")


@router.get("/health", response_class=HTMLResponse)
async def page_health(request: Request) -> HTMLResponse:
    return _page(request, "health.html", active="health")


@router.get("/history", include_in_schema=False)
async def page_history() -> RedirectResponse:
    return RedirectResponse("/patterns")  # the page this used to be


# -- JSON API ----------------------------------------------------------------


def _monitor(request: Request) -> dict[str, Any]:
    snap = poller_of(request).snapshot
    return {
        "online": snap.online,
        "login_disabled": snap.login_disabled,
        "error": snap.error,
        "updated_at": snap.updated_at,
        "poll_s": request.app.state.settings.poll_interval_seconds,
    }


@router.get("/api/now")
async def api_now(request: Request) -> dict:
    return dataclasses.asdict(poller_of(request).snapshot)


@router.get("/api/overview")
async def api_overview(request: Request, tz: ZoneInfo = Depends(tz_of)) -> dict:
    live = dataclasses.asdict(poller_of(request).snapshot)
    out = await db_of(request).insight(insights.overview, tz, now_of(request), live)
    out["monitor"] = _monitor(request)
    return out


@router.get("/api/patterns")
async def api_patterns(
    request: Request,
    subject: str = Query("devices", max_length=200),
    weeks: int = Query(8),
    tz: ZoneInfo = Depends(tz_of),
) -> dict:
    if weeks not in PATTERN_WEEKS:
        raise HTTPException(status_code=400, detail=f"weeks must be one of {PATTERN_WEEKS}")
    try:
        return await db_of(request).insight(insights.patterns, tz, now_of(request), subject, weeks)
    except insights.UnknownSubject as exc:
        raise HTTPException(status_code=404, detail="Unknown subject") from exc


@router.get("/api/devices")
async def api_devices(request: Request, tz: ZoneInfo = Depends(tz_of)) -> dict:
    return await db_of(request).insight(insights.devices, tz, now_of(request))


@router.get("/api/device-days")
async def api_device_days(
    request: Request, days: int = Query(90), tz: ZoneInfo = Depends(tz_of)
) -> dict:
    if days not in MATRIX_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {MATRIX_DAYS}")
    return await db_of(request).insight(insights.device_days, tz, now_of(request), days)


@router.get("/api/health")
async def api_health(
    request: Request, days: int = Query(30), tz: ZoneInfo = Depends(tz_of)
) -> dict:
    if days not in HEALTH_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {HEALTH_DAYS}")
    out = await db_of(request).insight(insights.health, tz, now_of(request), days)
    out["monitor"] = _monitor(request)
    return out


@router.post("/api/devices")
async def api_update_devices(request: Request, body: DevicesUpdate) -> dict:
    """Apply one edit to several MAC addresses (a device can have many)."""
    fields = body.model_dump(exclude_unset=True, exclude={"macs"})
    changed = await db_of(request).update_devices([m.upper() for m in body.macs], **fields)
    return {"changed": changed}


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


app.include_router(router)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
