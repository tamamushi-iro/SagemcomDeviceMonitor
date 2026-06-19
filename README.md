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

The whole app (dashboard + poller) runs in one container, configured entirely
with `BCDM_`-prefixed env vars — no `config.toml` needed.

```bash
cp .env.example .env                 # then edit .env
docker compose up -d --build
```

At minimum set `BCDM_ROUTER_PASSWORD` in `.env`, and change `BCDM_AUTH_PASSWORD`
and `BCDM_NTFY_TOPIC`. Open `http://<host>:8000` and log in with
`BCDM_AUTH_USER` / `BCDM_AUTH_PASSWORD`. Logs go to stdout
(`docker compose logs -f`).

History lives in SQLite on the named `bcdm-data` volume (mounted at `/data`), so
it survives `docker compose down` and image rebuilds. The same one-shot
utilities work in the container:

```bash
docker compose run --rm bcdm python -m bcdm --once         # connectivity check
docker compose run --rm bcdm python -m bcdm --test-alert   # send a test alert
```

Prefer a config file over env vars? Uncomment the `config.toml` mount in
`docker-compose.yml` and remove the `env_file` line. The container must be able
to reach your router's IP; on the default bridge network this works as long as
the Docker host can. If it can't, set `network_mode: host` (and drop the
`ports:` mapping).

## Deploy on the Raspberry Pi

The Pi reaches the Bell gateway directly, so no route/VPN workaround is needed.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv isn't installed
git clone <repo> ~/BellDeviceMonitor && cd ~/BellDeviceMonitor
uv sync && cp config.example.toml config.toml     # edit config.toml
```

Then install the service (edit paths/user inside it first):

```bash
sudo cp deploy/bcdm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bcdm
systemctl status bcdm        # journalctl -u bcdm -f  for logs
```
