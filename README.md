# musicdl

## Current implementation

The repository currently contains the Phase 0 foundation, the WeCom callback boundary, deterministic multi-source search, the Phase 3 Telegram user-account connector and music-Bot adapter contracts, the Phase 4 injected download/media-safety engine, and the Phase 6 restricted plugin runtime. Concrete provider adapters, AI ranking, and the management UI remain integration work or later phases.

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

The plugin runner is a restricted capability boundary, not a general-purpose Python or
JavaScript environment. Python plugins run in a one-shot subprocess with restricted
builtins, seccomp, resource limits, and no inherited environment. JavaScript plugins
run in Deno with all permissions denied. Plugins cannot read host/container files,
spawn processes, open sockets, or use the Docker socket. Network access is represented
by HTTPS actions handled by the main service's exact-host broker; the runner itself
has no egress.

A manifest names the language, operations, SHA-256 source digest, and exact allowed
hosts, for example:

```json
{"plugin_id":"example","version":"1","language":"python",
 "operations":["search"],"allowed_hosts":["api.example.com"],
 "sha256":"<64 lowercase hex characters>"}
```

The supported entry point is a `handle(request)` function. A plugin may return a
JSON-compatible result or one HTTPS `GET` action at a time; the main action loop
supplies bounded observations. Source is limited to 128 KiB, payload/result to 64 KiB,
invocations to 6 MiB, and at most four HTTP actions/observations. Jobs are limited to
30 seconds wall time, 5 seconds CPU, 256 MiB Python address space (128 MiB Deno heap),
1 MiB file size, 32 file descriptors, 64 KiB stdout, and 16 KiB stderr.

Incompatibilities are intentional: plugins cannot import arbitrary packages, access
environment variables or secrets, persist files, use arbitrary URLs/redirects/proxies,
use credentials in URLs, or call the main service directly. Docker runtime checks are
required for release; this Windows checkout can only report the static/unit evidence
when Docker Engine is unavailable.

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

Only `musicdl` publishes a loopback port. `plugin-runner` is attached only to the internal `plugin-control` network; the main service is attached to both the default and plugin-control networks. Plugin execution is enabled only through the restricted runner and main-owned HTTPS action broker described above.

## Verification

Run the strict local test suite with:

```bash
python -m pytest -q -W error
```

The health tests use the ASGI application directly and the Compose tests parse `compose.yaml` with PyYAML. Runtime Docker verification must be performed on a Docker-capable deployment host.
