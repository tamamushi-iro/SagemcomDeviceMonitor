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
```

## Run with Docker

```bash
cp .env.example .env                 # then edit .env
docker compose up -d --build
```

```bash
docker compose run --rm bcdm python -m bcdm --once         # connectivity check
docker compose run --rm bcdm python -m bcdm --test-alert   # send a test alert
```
