"""Background poller: query the router, persist, alert. Runs for the app lifetime.

Resilient by design -- any failure (router reboot, session expiry, network blip)
is caught, surfaced on the snapshot, and retried with exponential backoff so the
service never crashes. Holds the latest enriched "now" snapshot in memory for the
dashboard's live view.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from sagemcom_api.exceptions import (
    AuthenticationException,
    LoginRetryErrorException,
    MaximumSessionCountException,
)

from .alerts import Alerter
from .config import Settings
from .db import Database
from .router_client import RouterClient
from .util import to_iso_z, utc_now, utc_now_str

log = logging.getLogger(__name__)

_MAX_BACKOFF = 60.0
# Wrong credentials / router lockout: retry slowly. Hammering a bad login only
# extends the Sagemcom lockout (XMO_LOGIN_RETRY_ERR), so we wait this long.
_AUTH_BACKOFF = 120.0
_AUTH_ERRORS = (AuthenticationException, LoginRetryErrorException, MaximumSessionCountException)


@dataclass
class Snapshot:
    updated_at: str | None = None
    active_count: int = 0
    total_count: int = 0
    devices: list[dict[str, Any]] = field(default_factory=list)
    online: bool = False
    error: str | None = None


class Poller:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        router: RouterClient,
        alerter: Alerter,
    ):
        self._s = settings
        self._db = db
        self._router = router
        self._alerter = alerter
        self.snapshot = Snapshot()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_prune_day: str | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="bcdm-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        await self._router.close()

    async def run(self) -> None:
        interval = max(self._s.poll_interval_seconds, 5.0)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                hosts = await self._router.get_hosts()
                events, now_devices = await self._db.apply_poll(hosts)
                self.snapshot = Snapshot(
                    updated_at=to_iso_z(utc_now_str()),
                    active_count=len(now_devices),
                    total_count=len(hosts),
                    devices=now_devices,
                    online=True,
                )
                for event in events:
                    await self._alerter.handle_event(event, self._s.alert_new_device)
                await self._maybe_prune()
                backoff = 1.0
                await self._wait(interval)
            except asyncio.CancelledError:
                raise
            except _AUTH_ERRORS as exc:
                log.warning(
                    "Login rejected (%s); backing off %.0fs. Check router_password/encryption.",
                    exc, _AUTH_BACKOFF,
                )
                self.snapshot.online = False
                self.snapshot.error = f"login rejected: {exc}"
                await self._wait(_AUTH_BACKOFF)
                backoff = 1.0
            except Exception as exc:  # pylint: disable=broad-except
                log.warning("Poll failed: %s", exc)
                self.snapshot.online = False
                self.snapshot.error = str(exc)
                await self._wait(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _wait(self, seconds: float) -> None:
        """Sleep, but wake immediately if stop() is called."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _maybe_prune(self) -> None:
        today = utc_now().date().isoformat()
        if self._last_prune_day == today:
            return
        self._last_prune_day = today
        deleted = await self._db.prune(self._s.retention_days)
        if deleted:
            log.info("Pruned %d count samples older than %d days", deleted, self._s.retention_days)
