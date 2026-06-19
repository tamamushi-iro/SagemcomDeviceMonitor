"""Command-line entry point.

    python -m bcdm                # run the dashboard (default)
    python -m bcdm --once         # poll the router once, print hosts, exit
    python -m bcdm --test-alert   # send a test ntfy notification and exit
"""

from __future__ import annotations

import argparse
import asyncio
import os

from .alerts import Alerter
from .config import get_settings
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


async def _once() -> None:
    settings = get_settings()
    client = RouterClient(settings)
    try:
        hosts = await client.get_hosts()
    finally:
        await client.close()
    active = [h for h in hosts if h["active"]]
    print(f"{len(active)} / {len(hosts)} hosts active on {settings.router_host}\n")
    for h in sorted(active, key=lambda x: x.get("active_last_change") or "", reverse=True):
        print(f"  {h['mac']:<18} {h['ip'] or '-':<15} {h['interface_type'] or '-':<14} {h['name']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bcdm", description="BellDeviceMonitor")
    parser.add_argument("--host", default=os.environ.get("BCDM_BIND_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BCDM_BIND_PORT", "8000")))
    parser.add_argument("--once", action="store_true",
                        help="poll the router once, print connected hosts, and exit")
    parser.add_argument("--test-alert", action="store_true",
                        help="send a test ntfy notification and exit")
    args = parser.parse_args(argv)

    if args.test_alert:
        asyncio.run(_test_alert())
        return
    if args.once:
        asyncio.run(_once())
        return

    import uvicorn

    uvicorn.run("bcdm.web.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
