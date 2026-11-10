"""Background poller: query the router, persist, alert. Runs for the app lifetime.

Resilient by design -- any failure (router reboot, session expiry, network blip)
is caught, surfaced on the snapshot, and retried with exponential backoff so the
service never crashes. Holds the latest enriched "now" snapshot in memory for the
dashboard's live view. The one thing it does not retry is a password the router
keeps rejecting: the LoginGuard disables logins and the poller just reports that.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from sagemcom_api.exceptions import (
    LoginRetryErrorException,
    MaximumSessionCountException,
    UnauthorizedException,
)

from .alerts import Alerter
from .config import Settings
from .db import Database
from .login_guard import LoginDisabledError
from .router_client import RouterClient
from .util import to_iso_z, utc_now, utc_now_str

log = logging.getLogger(__name__)

_MAX_BACKOFF = 60.0
# Rejected login / too many router sessions: wait this long before the next try.
# For a rejected password that next try is the only retry: the LoginGuard disables
# logins on the second rejection (or at once on a lockout).
_AUTH_BACKOFF = 120.0
_AUTH_ERRORS = (UnauthorizedException, LoginRetryErrorException, MaximumSessionCountException)


@dataclass
class Snapshot:
    updated_at: str | None = None
    active_count: int = 0
    total_count: int = 0
    devices: list[dict[str, Any]] = field(default_factory=list)
    online: bool = False
    error: str | None = None
    login_disabled: bool = False


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
        self._login_disabled_alerted = False

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
                self._login_disabled_alerted = False
                for event in events:
                    await self._alerter.handle_event(event, self._s.alert_new_device)
                await self._maybe_prune()
                backoff = 1.0
                await self._wait(interval)
            except asyncio.CancelledError:
                raise
            except LoginDisabledError as exc:
                # Gave up on the router login. Stay up (exiting would only make
                # systemd/Docker restart us), keep serving history, and re-check each
                # interval: the guard answers from its file without contacting the
                # router, so `--reset-login` resumes polling without a restart.
                self._offline(str(exc), login_disabled=True)
                if not self._login_disabled_alerted:
                    self._login_disabled_alerted = True
                    log.error("%s", exc)
                    await self._alerter.login_disabled(str(exc))
                backoff = 1.0
                await self._wait(interval)
            except _AUTH_ERRORS as exc:
                if isinstance(exc, MaximumSessionCountException):
                    what = "router has too many open sessions"  # not counted by the guard
                else:
                    what = "router rejected the login (one more rejection disables logins)"
                log.warning("%s: %s; retrying in %.0fs", what, exc, _AUTH_BACKOFF)
                self._offline(f"{what}: {exc}; retrying in {_AUTH_BACKOFF:.0f}s")
                await self._wait(_AUTH_BACKOFF)
                backoff = 1.0
            except Exception as exc:  # pylint: disable=broad-except
                log.warning("Poll failed: %s", exc)
                self._offline(str(exc))
                await self._wait(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def _offline(self, error: str, *, login_disabled: bool = False) -> None:
        """Mark the live snapshot offline. Any outcome other than "logins disabled"
        ends a disabled episode, so if logins get disabled again it alerts again."""
        self.snapshot.online = False
        self.snapshot.error = error
        self.snapshot.login_disabled = login_disabled
        if not login_disabled:
            self._login_disabled_alerted = False

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
