"""Command-line entry point.

    python -m bcdm                        # run the dashboard (default)
    python -m bcdm --once                 # poll the router once, print hosts, exit
    python -m bcdm --test-alert           # send a test ntfy notification and exit
    python -m bcdm --reset-login          # re-enable router logins after they were disabled
    python -m bcdm --forget-owner NAME    # delete a person's devices and their history
    python -m bcdm --forget-device NAME   # delete one device (by name or MAC) and its history
    python -m bcdm --drop-interface-samples   # remove the unused table of older databases

The last three only show what they would delete unless --yes is added. They are
safe to run while the monitor is running; it notices on its next page load.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

from sagemcom_api.exceptions import UnauthorizedException

from .alerts import Alerter
from .config import get_settings
from .db import LEGACY_TABLE, Database
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


async def forget(db: Database, kind: str, text: str, yes: bool) -> int:
    """Show, and with ``yes`` delete, an owner's devices or one device. Exit code."""
    now = int(time.time())
    matches = await db.find_subjects(kind, text, now)
    if not matches:
        print(f"No {kind} matches {text!r}; nothing deleted.")
        return 1
    if len(matches) > 1:
        print(f"{len(matches)} devices match {text!r}. Name one by a MAC address or its exact name:")
        for m in matches:
            print(f"  {m['name']:<30} {m['addresses'][0]['mac']}")
        return 1
    (match,) = matches
    addresses = match["addresses"]
    events = sum(a["events"] for a in addresses)
    print(f"{match['name']}: {len(addresses)} MAC address(es), {events} recorded events")
    for a in addresses:
        print(f"  {a['mac']}  {a['name'] or '-':<28} {a['events']:>6} events")
    if not yes:
        print("Nothing deleted. Add --yes to delete these and their history; it cannot be undone.")
        return 0
    done = await db.forget_subject(match["subject"], now)
    print(f"Deleted {done['addresses']} address(es) and {done['events']} events.")
    print("If one of them connects again it is tracked as a new device.")
    return 0


async def drop_interface_samples(db: Database, yes: bool) -> int:
    found = await db.drop_legacy_table(dry_run=not yes)
    if not found["present"]:
        print(f"This database has no {LEGACY_TABLE} table; nothing to drop.")
    elif not yes:
        print(f"{LEGACY_TABLE} holds {found['rows']} rows that nothing reads. Add --yes to drop it.")
    else:
        print(f"Dropped {LEGACY_TABLE}: {found['rows']} rows, {found['bytes'] / 1e6:.1f} MB freed.")
        print("The file keeps its size; the freed space is reused before the file grows again.")
    return 0


async def _with_db(action, *args) -> int:
    db = Database(get_settings().db_file)
    try:
        return await action(db, *args)
    finally:
        db.close()


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
    parser.add_argument("--forget-owner", metavar="NAME",
                        help="delete every device of this owner, with their history")
    parser.add_argument("--forget-device", metavar="NAME_OR_MAC",
                        help="delete one device (all of its MAC addresses), with its history")
    parser.add_argument("--drop-interface-samples", action="store_true",
                        help="remove the unused connection-type table left in older databases")
    parser.add_argument("--yes", action="store_true",
                        help="carry out a --forget-* or --drop-* instead of only showing it")
    args = parser.parse_args(argv)

    if args.reset_login:
        _reset_login()
        return
    if args.test_alert:
        asyncio.run(_test_alert())
        return
    if args.once:
        raise SystemExit(asyncio.run(_once()))
    if args.forget_owner:
        raise SystemExit(asyncio.run(_with_db(forget, "owner", args.forget_owner, args.yes)))
    if args.forget_device:
        raise SystemExit(asyncio.run(_with_db(forget, "device", args.forget_device, args.yes)))
    if args.drop_interface_samples:
        raise SystemExit(asyncio.run(_with_db(drop_interface_samples, args.yes)))

    import uvicorn

    uvicorn.run("bcdm.web.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
