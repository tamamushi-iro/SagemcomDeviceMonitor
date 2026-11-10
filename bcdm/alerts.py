"""ntfy push notifications.

Alerts are driven by presence *transitions* (see the poller), so a flapping
device cannot spam: one notification per state change. Dynamic text (device
names) goes in the UTF-8 body, never in HTTP headers, which must stay ASCII.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, settings: Settings):
        self._s = settings
        self._url = f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}"

    @property
    def configured(self) -> bool:
        return bool(self._s.ntfy_topic and self._s.ntfy_server)

    async def _post(
        self,
        *,
        title: str,
        message: str,
        priority: str = "default",
        tags: Iterable[str] | None = None,
    ) -> bool:
        if not self.configured:
            log.debug("ntfy not configured; skipping alert: %s", title)
            return False
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = ",".join(tags)
        if self._s.ntfy_token:
            headers["Authorization"] = f"Bearer {self._s.ntfy_token}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self._url, content=message.encode("utf-8"), headers=headers
                )
                resp.raise_for_status()
            return True
        except Exception as exc:  # pylint: disable=broad-except
            log.warning("Failed to send ntfy alert %r: %s", title, exc)
            return False

    async def handle_event(self, event: dict[str, Any], alert_new: bool) -> None:
        """Dispatch a presence event to ntfy per the configured rules."""
        kind = event["event"]
        name = event.get("name") or event["mac"]
        mac = event["mac"]
        ip = event.get("ip") or "—"
        if kind == "new" and alert_new:
            await self._post(
                title="New device on network",
                message=f"{name}\nMAC: {mac}\nIP: {ip}",
                priority="high",
                tags=["warning"],
            )
        elif kind == "online" and event.get("watched"):
            await self._post(
                title="Watched device online",
                message=f"{name} ({mac}) is back online",
                tags=["green_circle"],
            )
        elif kind == "offline" and event.get("watched"):
            await self._post(
                title="Watched device offline",
                message=f"{name} ({mac}) went offline",
                tags=["red_circle"],
            )

    async def login_disabled(self, reason: str) -> None:
        """Tell the user the monitor stopped logging in to the router (sent once)."""
        await self._post(
            title="Router login disabled",
            message=reason,
            priority="high",
            tags=["warning"],
        )

    async def test(self) -> bool:
        """Send a one-off test notification (used by `python -m bcdm --test-alert`)."""
        return await self._post(
            title="BellDeviceMonitor",
            message="ntfy is configured correctly.",
            tags=["bell"],
        )
