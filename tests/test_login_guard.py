"""Login-guard tests against a fake Sagemcom router; the real router is never contacted.

Run from the project root:  uv run python -m unittest discover -s tests -v

The fake checks every request's auth-key the way the gateway does, so a wrong
password is rejected exactly as the real router rejects it. The tests drive the real
sagemcom-api client, including its internal re-login retries, so re-run them after
upgrading sagemcom-api.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sagemcom_api.exceptions import AuthenticationException, MaximumSessionCountException

import bcdm.poller as poller_mod
from bcdm.config import Settings
from bcdm.db import Database
from bcdm.login_guard import MAX_FAILED_LOGINS, LoginDisabledError, LoginGuard
from bcdm.poller import Poller
from bcdm.router_client import RouterClient

GOOD, BAD = "good-password", "bad-password"
NO_ERR = {"code": 16777216, "description": "XMO_NO_ERR"}
HOSTS = [{"uid": 1, "PhysAddress": "aa:bb:cc:00:00:01", "IPAddress": "192.168.2.10",
          "HostName": "phone", "Active": True, "InterfaceType": "WiFi",
          "ActiveLastChange": "2026-11-09T20:00:00-0500"}]


def _sha512(text: str) -> str:
    return hashlib.sha512(text.encode()).hexdigest()


class FakeRouter:
    """Just enough Sagemcom JSON-req: logIn, getValue (always HOSTS), logOut."""

    def __init__(self):
        self.password = GOOD
        self.mode = "ok"  # or "lockout" / "max_sessions"
        self.logins = 0
        self.failed_logins = 0
        self.sessions: dict[int, str] = {}
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                req = json.loads(urllib.parse.parse_qs(body)["req"][0])["request"]
                out = json.dumps(fake.handle(req)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def change_password(self, new: str, drop_sessions: bool) -> None:
        """Like changing the admin password in the GUI, or swapping the modem."""
        with self._lock:
            self.password = new
            if drop_sessions:
                self.sessions.clear()

    def _signed(self, req: dict, nonce: str) -> bool:
        cred = _sha512(f"admin:{nonce}:{_sha512(self.password)}")
        return req["auth-key"] == _sha512(f"{cred}:{req['id']}:{req['cnonce']}:JSON:/cgi/json-req")

    def handle(self, req: dict) -> dict:
        def reply(desc, actions):
            return {"reply": {"uid": 0, "id": req["id"], "error": {"code": 0, "description": desc},
                              "actions": actions, "events": []}}

        def ok(params):
            return reply("XMO_REQUEST_NO_ERR", [{"uid": 1, "id": 0, "error": NO_ERR, "callbacks": [
                {"uid": 1, "result": NO_ERR, "xpath": "", "parameters": params}]}])

        def action_error(desc):
            return reply("XMO_REQUEST_ACTION_ERR", [{"uid": 1, "id": 0, "callbacks": [],
                                                     "error": {"code": 0, "description": desc}}])

        method = req["actions"][0]["method"]
        sid = int(req["session-id"])
        with self._lock:
            if method == "logIn":
                self.logins += 1
                if self.mode == "max_sessions":
                    return action_error("XMO_MAX_SESSION_COUNT_ERR")
                if self.mode == "lockout":
                    self.failed_logins += 1
                    return action_error("XMO_LOGIN_RETRY_ERR")
                if not any(self._signed(req, n) for n in ("", self.sessions.get(sid, ""))):
                    self.failed_logins += 1
                    return action_error("XMO_AUTHENTICATION_ERR")
                new_sid = 1000 + self.logins
                self.sessions[new_sid] = f"{new_sid}42"
                return ok({"id": new_sid, "nonce": self.sessions[new_sid]})
            if sid not in self.sessions:
                return reply("XMO_INVALID_SESSION_ERR", [])
            if not self._signed(req, self.sessions[sid]):
                return action_error("XMO_AUTHENTICATION_ERR")
            return ok({"value": HOSTS} if method == "getValue" else {})


class LoginGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = FakeRouter()
        self.addCleanup(self.router.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name, "test.db")

    def settings(self, password: str = GOOD) -> Settings:
        # Explicit values beat config.toml, .env and BCDM_* env vars: nothing real is used.
        return Settings(router_host=self.router.host, router_user="admin", router_password=password,
                        encryption="SHA512", verify_ssl=False, db_path=str(self.db_path),
                        ntfy_server="http://127.0.0.1:9", ntfy_topic="", ntfy_token="",
                        manage_dev_route=False)

    async def poll(self, password: str = GOOD):
        """One poll by a fresh client; also stands in for a restart or a --once run."""
        rc = RouterClient(self.settings(password))
        try:
            return await rc.get_hosts()
        finally:
            await rc.close()

    async def test_rejected_password_is_retried_once_then_never_again(self):
        with self.assertRaises(AuthenticationException):
            await self.poll(BAD)
        with self.assertRaises(LoginDisabledError) as cm:
            await self.poll(BAD)
        self.assertIn("rejected the configured admin password 2 times", str(cm.exception))
        for _ in range(3):  # later polls, restarts, --once runs: the router isn't contacted
            with self.assertRaises(LoginDisabledError):
                await self.poll(BAD)
        self.assertEqual(self.router.logins, MAX_FAILED_LOGINS)

    async def test_new_password_is_tried_and_clears_the_block(self):
        for exc in (AuthenticationException, LoginDisabledError):
            with self.assertRaises(exc):
                await self.poll(BAD)
        hosts = await self.poll(GOOD)
        self.assertEqual([h["mac"] for h in hosts], ["AA:BB:CC:00:00:01"])
        self.assertFalse(LoginGuard(self.settings()).path.exists())

    async def test_reset_allows_the_same_password_again(self):
        for exc in (AuthenticationException, LoginDisabledError):
            with self.assertRaises(exc):
                await self.poll(BAD)
        self.assertEqual(LoginGuard(self.settings(BAD)).reset(), "current")
        with self.assertRaises(AuthenticationException):  # a fresh try, not a refusal
            await self.poll(BAD)
        self.assertEqual(self.router.failed_logins, MAX_FAILED_LOGINS + 1)

    async def test_lockout_disables_logins_at_once(self):
        self.router.mode = "lockout"
        for _ in range(3):
            with self.assertRaises(LoginDisabledError) as cm:
                await self.poll(GOOD)
        self.assertIn("logins are locked", str(cm.exception))
        self.assertEqual(self.router.logins, 1)

    async def test_too_many_sessions_is_not_counted(self):
        self.router.mode = "max_sessions"
        for _ in range(3):
            with self.assertRaises(MaximumSessionCountException):
                await self.poll(GOOD)
        self.router.mode = "ok"
        self.assertTrue(await self.poll(GOOD))

    async def _password_changed_while_logged_in(self, drop_sessions: bool):
        # sagemcom-api re-logs in by itself after a mid-session auth/session error.
        # Those attempts must count too, or a single poll could burn several tries.
        rc = RouterClient(self.settings(GOOD))
        try:
            await rc.get_hosts()
            self.router.change_password("changed-in-the-gui", drop_sessions=drop_sessions)
            for _ in range(3):
                with self.assertRaises(LoginDisabledError):
                    await rc.get_hosts()
        finally:
            await rc.close()
        self.assertEqual(self.router.failed_logins, MAX_FAILED_LOGINS)

    async def test_password_changed_while_logged_in_sessions_dropped(self):
        await self._password_changed_while_logged_in(drop_sessions=True)

    async def test_password_changed_while_logged_in_session_kept(self):
        await self._password_changed_while_logged_in(drop_sessions=False)

    async def test_guard_file_does_not_contain_the_password(self):
        with self.assertRaises(AuthenticationException):
            await self.poll(BAD)
        text = LoginGuard(self.settings(BAD)).path.read_text(encoding="utf-8")
        self.assertNotIn(BAD, text)

    async def test_poller_gives_up_alerts_once_and_resumes_after_reset(self):
        alerts: list[str] = []

        class Alerts:
            async def handle_event(self, event, alert_new):
                pass

            async def login_disabled(self, reason):
                alerts.append(reason)

        s = self.settings(BAD)
        db = Database(s.db_file)
        poller = Poller(s, db, RouterClient(s), Alerts())
        # Run the loop fast, but hold the retry wait long enough to observe that state.
        poller._wait = lambda seconds: asyncio.sleep(0.2 if seconds == poller_mod._AUTH_BACKOFF else 0.01)
        poller.start()
        try:
            await self._until(lambda: poller.snapshot.login_disabled)
            await asyncio.sleep(0.3)  # many more polling cycles while disabled
            self.assertEqual(self.router.failed_logins, MAX_FAILED_LOGINS)
            self.assertEqual(len(alerts), 1)
            self.assertFalse(poller.snapshot.online)
            self.assertIn("Router login disabled", poller.snapshot.error)

            # Reset while the password is still wrong: one more round, a fresh alert,
            # and the dashboard must not claim "disabled" during the retry wait.
            LoginGuard(s).reset()
            await self._until(lambda: not poller.snapshot.login_disabled)
            self.assertIn("router rejected the login", poller.snapshot.error)
            await self._until(lambda: len(alerts) == 2)
            self.assertTrue(poller.snapshot.login_disabled)
            self.assertEqual(self.router.failed_logins, 2 * MAX_FAILED_LOGINS)

            self.router.change_password(BAD, drop_sessions=False)  # the router accepts it now
            LoginGuard(s).reset()  # what `python -m bcdm --reset-login` does
            await self._until(lambda: poller.snapshot.online)
            self.assertFalse(poller.snapshot.login_disabled)
        finally:
            await poller.stop()
            db.close()

    async def _until(self, predicate, timeout: float = 10.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                self.fail("condition not reached in time")
            await asyncio.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
