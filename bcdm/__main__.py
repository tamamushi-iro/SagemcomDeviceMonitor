"""Command-line entry point.

    python -m bcdm                # run the dashboard (default)
    python -m bcdm --once         # poll the router once, print hosts, exit
    python -m bcdm --test-alert   # send a test ntfy notification and exit
    python -m bcdm --reset-login  # re-enable router logins after they were disabled
"""

from __future__ import annotations

import argparse
import asyncio
import os

from sagemcom_api.exceptions import UnauthorizedException

from .alerts import Alerter
from .config import get_settings
from .login_guard import MAX_FAILED_LOGINS, LoginDisabledError, LoginGuard
from .router_client import RouterClient


async def _test_alert() -> None:
    settings = get_settings()
    alerter = Alerter(settings)
    if not alerter.configured:
        print("ntfy is not configured (set ntfy_topic in config.toml).")
        return
    ok = await alerter.test()
    target = f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}"
    print(f"Test alert -> {target}: {'sent' if ok else 'FAILED (see logs)'}")


async def _once() -> int:
    settings = get_settings()
    client = RouterClient(settings)
    try:
        hosts = await client.get_hosts()
    except LoginDisabledError as exc:
        print(exc)
        return 2
    except UnauthorizedException as exc:
        print(f"The router rejected the login ({exc}). Check router_password: after "
              f"{MAX_FAILED_LOGINS} rejections router logins are disabled, to avoid a lockout.")
        return 1
    finally:
        await client.close()
    active = [h for h in hosts if h["active"]]
    print(f"{len(active)} / {len(hosts)} hosts active on {settings.router_host}\n")
    for h in sorted(active, key=lambda x: x.get("active_last_change") or "", reverse=True):
        print(f"  {h['mac']:<18} {h['ip'] or '-':<15} {h['interface_type'] or '-':<14} {h['name']}")
    return 0


def _reset_login() -> None:
    result = LoginGuard(get_settings()).reset()
    if result == "current":
        print("Router logins re-enabled. A running monitor will try again on its next poll.")
    elif result == "other":
        print("Cleared a login block recorded for different router credentials than the "
              "current ones. If you expected these to match, check which config is in effect: "
              "BCDM_* environment variables override .env, which overrides config.toml.")
    else:
        print("No rejected logins recorded; nothing to reset.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bcdm", description="BellDeviceMonitor")
    parser.add_argument("--host", default=os.environ.get("BCDM_BIND_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BCDM_BIND_PORT", "8000")))
    parser.add_argument("--once", action="store_true",
                        help="poll the router once, print connected hosts, and exit")
    parser.add_argument("--test-alert", action="store_true",
                        help="send a test ntfy notification and exit")
    parser.add_argument("--reset-login", action="store_true",
                        help="re-enable router logins disabled after rejected passwords or a lockout")
    args = parser.parse_args(argv)

    if args.reset_login:
        _reset_login()
        return
    if args.test_alert:
        asyncio.run(_test_alert())
        return
    if args.once:
        raise SystemExit(asyncio.run(_once()))

    import uvicorn

    uvicorn.run("bcdm.web.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
