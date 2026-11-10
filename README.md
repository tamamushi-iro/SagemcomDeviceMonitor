# BellDeviceMonitor

A monitor + web dashboard for the devices connected to a Bell (Sagemcom F@st) home gateway.

## Setup

Requires Python 3.14 (managed automatically by [`uv`](https://docs.astral.sh/uv/)).

```bash
uv sync                              # create the venv + install deps
cp config.example.toml config.toml   # then edit config.toml (see below)
```

## Run

```bash
uv run uvicorn bcdm.web.app:app --host 0.0.0.0 --port 8000
# or: uv run python -m bcdm
```

Open `http://<host>:8000` and log in with `auth_user` / `auth_password`.

Handy utilities:

```bash
uv run python -m bcdm --once         # one-shot: print connected hosts (connectivity check)
uv run python -m bcdm --test-alert   # send a test ntfy notification
uv run python -m bcdm --reset-login  # re-enable router logins (see "Router login protection")
```

## Run with Docker

```bash
cp .env.example .env                 # then edit .env
docker compose up -d --build
```

```bash
docker compose run --rm bcdm python -m bcdm --once          # connectivity check
docker compose run --rm bcdm python -m bcdm --test-alert    # send a test alert
docker compose run --rm bcdm python -m bcdm --reset-login   # re-enable router logins
```

## Router login protection

If the router rejects the admin password (e.g. the modem was replaced or its password
changed), the monitor tries once more, then stops logging in so it can't trigger the
router's login lockout. If the router reports a lockout, logins stop at once.
The monitor keeps running and serving history; the dashboard shows the reason and the fix,
and one ntfy alert is sent. `--once` and `scripts/` share the same limit.

The stop is remembered across restarts for that exact password (a salted hash in
`bcdm.login-guard.json`, next to the database). To clear it, either set the new
`router_password` and restart, or keep the password and run `--reset-login` (e.g. after
power-cycling a locked modem). A running monitor picks up a reset on its next poll.

## Tests

```bash
uv run python -m unittest discover -s tests -v   # fake router only; never contacts the real one
```

## Notes

- [DHCP reservations via `sagemcom-api`](docs/dhcp-reservations.md) — can we set a host
  to a reserved IP through the library? (short version: writes roll back on this Bell
  gateway; it's effectively read-only over the API)
- [Reading device state from the gateway](docs/reading-device-state.md) — interpreting
  host records (`address_source`, "visible but no IP"), randomized MACs, and WiFi
  association stats
