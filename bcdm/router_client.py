"""Resilient wrapper around sagemcom-api for long-running polling.

Owns a single :class:`SagemcomClient` session, logs in lazily, retries once on a
dropped/expired session, and normalizes :class:`Device` objects into plain dicts.
On unrecoverable failure it closes the session and re-raises so the poller backs off.
Every login passes through a :class:`LoginGuard`, so a rejected password is never
retried indefinitely (see login_guard.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sagemcom_api.client import SagemcomClient
from sagemcom_api.enums import ApiMode, EncryptionMethod
from sagemcom_api.exceptions import LoginRetryErrorException, UnauthorizedException

from .config import Settings
from .login_guard import LoginDisabledError, LoginGuard

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


class GuardedSagemcomClient(SagemcomClient):
    """SagemcomClient whose every login goes through a LoginGuard.

    Overriding login() also covers sagemcom-api's own re-login retries (its backoff
    handler calls ``client.login()`` after a mid-session auth or session error), which
    would otherwise make extra, uncounted attempts with a password that has stopped
    working. Guard calls run in a thread: they hash the credentials (deliberately slow)
    and touch a file, which shouldn't stall the event loop.
    """

    def __init__(self, *args: Any, guard: LoginGuard, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._guard = guard

    async def login(self):
        await asyncio.to_thread(self._guard.check)
        try:
            result = await super().login()
        except LoginRetryErrorException as exc:  # the router has locked logins
            await asyncio.to_thread(self._guard.record_failure, exc, lockout=True)
            raise
        except UnauthorizedException as exc:  # includes AuthenticationException
            await asyncio.to_thread(self._guard.record_failure, exc)
            raise
        await asyncio.to_thread(self._guard.record_success)
        return result


def make_client(settings: Settings, guard: LoginGuard) -> GuardedSagemcomClient:
    """The one place router clients are built (the monitor, --once and scripts)."""
    return GuardedSagemcomClient(
        settings.router_host,
        settings.router_user,
        settings.router_password,
        _encryption(settings.encryption),
        verify_ssl=settings.verify_ssl,
        # The Bell gateway speaks the legacy JSON-req API. The library default (AUTO)
        # falls back to its experimental REST API when the router serves an HTML error
        # page (e.g. mid-reboot): that POSTs the plaintext password to /api/v1/login and
        # can switch the session to REST-shaped host data.
        api_mode=ApiMode.LEGACY,
        guard=guard,
    )


class RouterClient:
    def __init__(self, settings: Settings, guard: LoginGuard | None = None):
        self._s = settings
        self.guard = guard or LoginGuard(settings)
        self._client: SagemcomClient | None = None

    async def _connect(self) -> SagemcomClient:
        # Fail fast while disabled, without even opening an HTTP session.
        await asyncio.to_thread(self.guard.check)
        client = make_client(self._s, self.guard)
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
            except LoginDisabledError:
                await self.close()
                raise
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
