"""Optional dev-only network route helper (Windows).

On a dev box that reaches the router's subnet via a second router while a
full-tunnel VPN is active, a temporary static route is needed (this is the
workaround the original bcdm_v2.py carried). It is a NO-OP unless
``manage_dev_route`` is enabled in config, and only acts on Windows -- so on the
Raspberry Pi it never touches the routing table and needs no elevation.

Used as a context manager around the poller's lifetime in the app lifespan.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys

from .config import Settings

log = logging.getLogger(__name__)


def _is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
    except Exception:  # pylint: disable=broad-except
        return False


class DevRoute:
    def __init__(self, settings: Settings):
        self._s = settings
        self._active = False

    @property
    def enabled(self) -> bool:
        return self._s.manage_dev_route and sys.platform == "win32"

    def __enter__(self) -> "DevRoute":
        if not self.enabled:
            return self
        if not _is_admin():
            raise PermissionError(
                "manage_dev_route is enabled but this process is not elevated. "
                "Run from an elevated terminal, or set manage_dev_route = false."
            )
        self._delete()  # clear any stale route left by a crashed run
        subprocess.run(
            ["route", "add", self._s.dev_route_dest, "mask",
             self._s.dev_route_mask, self._s.dev_route_gw],
            check=True, capture_output=True, text=True,
        )
        self._active = True
        log.info(
            "Added dev route %s mask %s via %s",
            self._s.dev_route_dest, self._s.dev_route_mask, self._s.dev_route_gw,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._active:
            self._delete()
            self._active = False
            log.info("Removed dev route %s", self._s.dev_route_dest)

    def _delete(self) -> None:
        subprocess.run(
            ["route", "delete", self._s.dev_route_dest],
            check=False, capture_output=True, text=True,
        )
