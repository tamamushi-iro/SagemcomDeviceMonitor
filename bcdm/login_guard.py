"""Stop logging in to the router once it rejects our credentials.

Every router login goes through a :class:`LoginGuard` (see
``router_client.GuardedSagemcomClient``), including sagemcom-api's own internal
re-login retries. After ``MAX_FAILED_LOGINS`` rejections of the same credentials,
or as soon as the router reports a lockout, logins are disabled and
:class:`LoginDisabledError` is raised instead of contacting the router.

The state is a small JSON file next to the database, so it survives restarts and
is shared by the monitor, ``python -m bcdm --once`` and the scripts. It is keyed by
a salted, slow hash of the router credentials: changing ``router_password`` (or the
host/user/encryption) starts from a clean slate, and ``python -m bcdm --reset-login``
clears it by hand. The file is re-read before every login, so a reset reaches a
running monitor on its next poll.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from .config import Settings
from .util import utc_now_str

log = logging.getLogger(__name__)

# Rejections allowed per set of credentials: the first try plus one retry.
MAX_FAILED_LOGINS = 2
_KDF_ITERATIONS = 200_000


class LoginDisabledError(Exception):
    """Raised instead of contacting the router while logins are disabled."""


class LoginGuard:
    def __init__(self, settings: Settings, path: Path | None = None):
        self._s = settings
        self.path = path or settings.db_file.with_suffix(".login-guard.json")
        self._fingerprints: dict[str, str] = {}
        # Last state written. Only consulted when the file can't be written, so the
        # process still gives up on a read-only data directory.
        self._state: dict[str, Any] | None = None
        self._write_failed = False

    def check(self) -> None:
        """Raise LoginDisabledError if logins are disabled for these credentials."""
        state = self._load()
        if state and state.get("disabled"):
            raise LoginDisabledError(_describe(state))

    def record_failure(self, exc: BaseException, *, lockout: bool = False) -> None:
        """Count a rejected login; raise LoginDisabledError if that disables logins."""
        state = self._load() or {"salt": secrets.token_hex(16), "failures": 0}
        state["fingerprint"] = self._fingerprint(state["salt"])
        state["failures"] = int(state.get("failures", 0)) + 1
        state["last_failure_at"] = utc_now_str()
        state["last_error"] = str(exc)[:200]
        if lockout or state["failures"] >= MAX_FAILED_LOGINS:
            state["disabled"] = True
            state["reason"] = "lockout" if lockout else "rejected"
        self._save(state)
        if state.get("disabled"):
            raise LoginDisabledError(_describe(state)) from exc

    def record_success(self) -> None:
        """A login worked: forget any recorded failures."""
        self._state, self._write_failed = None, False
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove login guard %s: %s", self.path, exc)

    def reset(self) -> str | None:
        """Clear recorded failures by hand.

        Returns "current" if they were for the configured credentials, "other" if
        they were for different credentials, or None if nothing was recorded.
        """
        existed = self.path.exists() or self._state is not None
        current = self._load() is not None
        self._state, self._write_failed = None, False
        self.path.unlink(missing_ok=True)
        return "current" if current else ("other" if existed else None)

    def _fingerprint(self, salt_hex: str) -> str:
        """Salted, slow hash of the credentials, so the file doesn't expose the password."""
        if salt_hex not in self._fingerprints:
            s = self._s
            secret = "\0".join((s.router_host, s.router_user, s.router_password, s.encryption.upper()))
            self._fingerprints[salt_hex] = hashlib.pbkdf2_hmac(
                "sha256", secret.encode("utf-8"), bytes.fromhex(salt_hex), _KDF_ITERATIONS
            ).hex()
        return self._fingerprints[salt_hex]

    def _load(self) -> dict[str, Any] | None:
        """The recorded state if it belongs to the current credentials, else None."""
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = self._state if self._write_failed else None
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable login guard %s: %s", self.path, exc)
            state = self._state
        if not isinstance(state, dict):
            return None
        try:
            ours = secrets.compare_digest(str(state["fingerprint"]), self._fingerprint(str(state["salt"])))
        except (KeyError, ValueError):
            return None
        return state if ours else None

    def _save(self, state: dict[str, Any]) -> None:
        self._state = state
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._write_failed = False
        except OSError as exc:
            self._write_failed = True
            log.error("Could not save login guard %s (%s); it only holds until this process exits",
                      self.path, exc)


def _describe(state: dict[str, Any]) -> str:
    """The user-facing reason, shown on the dashboard, in the alert and by the CLI."""
    when = f"{state.get('last_failure_at', '?')} UTC"
    if state.get("reason") == "lockout":
        return (
            f"Router login disabled: at {when} the router reported that logins are locked "
            "(too many failed attempts). Trying again would only extend the lock.\n"
            "Fix: power-cycle the modem and check router_password, then run: "
            "python -m bcdm --reset-login"
        )
    return (
        f"Router login disabled: the router rejected the configured admin password "
        f"{state.get('failures')} times (last at {when}). The modem was probably replaced or "
        "its admin password changed, so BellDeviceMonitor stopped trying rather than get the "
        "router to lock logins.\n"
        "Fix: set router_password to the current password and restart (BCDM_* environment "
        "variables override .env, which overrides config.toml). To try the same password "
        "again, run: python -m bcdm --reset-login"
    )
