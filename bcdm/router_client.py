"""Resilient wrapper around sagemcom-api for long-running polling.

Owns a single :class:`SagemcomClient` session, logs in lazily, retries once on a
dropped/expired session, and normalizes :class:`Device` objects into plain dicts.
On unrecoverable failure it closes the session and re-raises so the poller backs off.
"""

from __future__ import annotations

import logging
from typing import Any

from sagemcom_api.client import SagemcomClient
from sagemcom_api.enums import EncryptionMethod

from .config import Settings

log = logging.getLogger(__name__)


def _encryption(method: str) -> EncryptionMethod:
    try:
        return EncryptionMethod(method.upper())
    except ValueError:
        log.warning("Unknown encryption method %r; defaulting to SHA512", method)
        return EncryptionMethod.SHA512


def host_to_dict(device: Any) -> dict[str, Any] | None:
    """Normalize a sagemcom ``Device`` to the dict shape the DB layer expects.

    Devices without a MAC are useless for presence tracking and are dropped.
    """
    mac = getattr(device, "phys_address", None)
    if not mac:
        return None
    return {
        "mac": mac.upper(),
        "name": device.name,  # property: user_host_name or host_name
        "ip": device.ip_address,
        "interface_type": device.interface_type,
        "active": bool(device.active),
        "active_last_change": device.active_last_change,
    }


class RouterClient:
    def __init__(self, settings: Settings):
        self._s = settings
        self._client: SagemcomClient | None = None

    async def _connect(self) -> SagemcomClient:
        client = SagemcomClient(
            self._s.router_host,
            self._s.router_user,
            self._s.router_password,
            _encryption(self._s.encryption),
            verify_ssl=self._s.verify_ssl,
        )
        try:
            await client.login()
        except BaseException:
            await client.close()
            raise
        log.info("Logged in to router at %s", self._s.router_host)
        return client

    async def get_hosts(self) -> list[dict[str, Any]]:
        """Return normalized host dicts, (re)connecting as needed.

        Tries once more with a fresh session if the first attempt fails (covers
        session expiry / router reboot). Re-raises if the retry also fails.
        """
        last_exc: Exception | None = None
        for attempt in (1, 2):
            if self._client is None:
                self._client = await self._connect()
            try:
                devices = await self._client.get_hosts()
                return [d for d in (host_to_dict(x) for x in devices) if d]
            except Exception as exc:  # pylint: disable=broad-except
                last_exc = exc
                await self.close()
                if attempt == 1:
                    log.warning("get_hosts failed (%s); reconnecting once", exc)
        assert last_exc is not None
        raise last_exc

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # pylint: disable=broad-except
                pass
            self._client = None
