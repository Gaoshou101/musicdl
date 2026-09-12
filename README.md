# musicdl

## Current implementation

The repository currently contains the Phase 0 foundation, the WeCom callback boundary, deterministic multi-source search, the Phase 3 Telegram user-account connector and music-Bot adapter contracts, the Phase 4 injected download/media-safety engine, and Phase 5 optional advisory ranking and language suggestions. AI is disabled by default and uses deterministic, redacted fallback behavior. Compatibility with a live provider remains deployment validation; no live provider call is claimed here. Concrete provider adapters, plugin execution, and the management UI remain integration work or later phases.

`compose.yaml` defines only `musicdl` and `plugin-runner`; Redis remains an external service. Copy `.env.example` to `.env` and set `MUSICDL_REDIS__URL` before starting Compose. Never put credentials in the example file or source tree.

The main service owns the media, application-data, and Telegram-session volumes. The plugin runner receives no Redis URL, session, environment file, API key, main configuration, Docker socket, or host path.

Health handlers do not contact Redis, the plugin runner, or other external dependencies. The container images nevertheless install the Python runtime dependencies required by FastAPI and the application.

## Prerequisites

- Docker Engine with the Compose plugin on a Debian-like Linux host for runtime deployment.
- An externally managed Redis instance reachable from the `musicdl` container.
- Python 3.12 and the repository virtual environment for local tests.

Create a local development environment and install development dependencies:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

The current development host does not provide Docker, so image build and container runtime behavior are not verified here. Compose and Dockerfile contracts are verified statically.

## Configuration

Copy `.env.example` to `.env` and replace only placeholders with deployment values. Do not commit `.env` or real credentials.

| Variable | Required | Description |
| --- | --- | --- |
| `MUSICDL_REDIS__URL` | Yes | External Redis URL; supports `redis://` and `rediss://`. |
| `MUSICDL_PORT` | No | Host loopback port; defaults to `8000`. |
| `MUSICDL_CONFIG__VERSION` | Compose-set | Configuration schema version, currently `1`. |
| `MUSICDL_MEDIA__ROOT` | Compose-set | Main media mount, `/data/music`. |
| `MUSICDL_TELEGRAM__SESSION_ROOT` | Compose-set | Restricted Telegram session mount, `/data/telegram-sessions`. |
| `MUSICDL_TELEGRAM__ENABLED` | No | Enables the Telegram user-account connector; defaults to `false`. |
| `MUSICDL_TELEGRAM__API_ID` | When enabled | Telegram application API ID. |
| `MUSICDL_TELEGRAM__API_HASH` | When enabled | Telegram application API hash; never commit it. |
| `MUSICDL_TELEGRAM__PROFILE` | No | Restricted session profile name; defaults to `default`. |
| `MUSICDL_PLUGIN__SERVICE_URL` | Compose-set | Internal plugin endpoint, `http://plugin-runner:8080`. |

The connector supports code login, 2FA, restart restoration, invalid-session and rate-limit states. `PublicTelegramBot` supplies the built-in `/search {query}` command contract, while `CustomTelegramBot` accepts a validated command template. Raw Bot responses are decoded at the connector boundary and normalized into the shared candidate model. Real account credentials, the chosen public Bot, and any custom Bot response decoder must be supplied and validated in the deployment environment.

## Compose startup and health

```bash
cp .env.example .env
# Edit .env and set MUSICDL_REDIS__URL.
docker compose config
docker compose build
docker compose up -d
docker compose ps
curl http://127.0.0.1:${MUSICDL_PORT:-8000}/healthz
docker compose logs --tail=100 musicdl plugin-runner
```

Only `musicdl` publishes a loopback port. `plugin-runner` is attached only to the internal `plugin-control` network; the main service is attached to both the default and plugin-control networks. Plugin execution and external plugin egress remain disabled until Phase 6 establishes controlled egress.

## Verification

Run the strict local test suite with:

```bash
python -m pytest -q -W error
```

The health tests use the ASGI application directly and the Compose tests parse `compose.yaml` with PyYAML. Runtime Docker verification must be performed on a Docker-capable deployment host.
