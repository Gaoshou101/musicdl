<h1 align="center">musicdl</h1>

<p align="center">A local, multi-source music search and download service that serves a small WeCom allowlist, files every download into a four-category library, and can be driven from its own admin panel.</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"></a>
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/FastAPI-0.141.1-3776AB?style=flat-square" alt="FastAPI 0.141.1"></a>
  <a href="./compose.yaml"><img src="https://img.shields.io/badge/Docker-Compose-4B5563?style=flat-square" alt="Docker Compose"></a>
  <a href="./.env.example"><img src="https://img.shields.io/badge/Redis-external-4B5563?style=flat-square" alt="External Redis"></a>
  <a href="./docker/plugin/Dockerfile"><img src="https://img.shields.io/badge/plugin%20runtime-sandboxed-B91C1C?style=flat-square" alt="The plugin runtime is sandboxed"></a>
</p>

## Highlights

| Highlight | Why it matters |
|---|---|
| Search and selection both happen inside WeCom | A user sends `/search <query>`, or plain text, and replies with a number. Parsing lives in `musicdl.wecom.commands`; the selection reply is bound to one immutable candidate snapshot. |
| Every enabled source is queried at once | `search_sources` fans out with a per-source timeout, then deduplicates and orders by registry priority, lossless format, bitrate, metadata completeness, and size. |
| Downloads are verified before they are published | The container is re-derived from the response bytes, so an upstream that serves a real FLAC stream as `audio/mpeg` is caught rather than trusted. Files land in `<media root>/<category>/<artist>/<title> - <artist>.<ext>`. |
| A failed source is excluded instead of ending the request | The worker health-checks that source once, persists the refreshed candidates, and sends one new selection prompt. It never downloads a replacement on its own. |
| Untrusted source plugins run without credentials | `plugin-runner` has no Redis URL, no Telegram session, no API key, no Docker socket, and only the internal `plugin-control` network. Python plugins additionally run under seccomp and resource limits. |
| AI only advises, and ships disabled | With `MUSICDL_AI__ENABLED=false`, ranking and language classification stay deterministic. An enabled advisor that fails leaves the deterministic verdict in place. |
| The panel exercises the sources on its own | `GET /admin/search`, `POST /admin/download`, and `GET /admin/media/{path}` run the registry the workers run, so a source is proved out without sending the bot a message. |
| Dependency health separates "nothing to do" from "broken" | `/admin/health` answers `ok`, `failed`, or `not_required`, so a dependency with nothing to do in this deployment does not turn a working panel red. |
| The running configuration is editable from the panel | `GET /admin/config` and `PATCH /admin/config` move the WeCom, Redis, Telegram, AI, and worker budgets out of the host's `.env` and into the panel, where a change is saved once and the container is not rebuilt; secrets are write-only, and the settings only Compose owns say so. |
| Channel health answers "which source is broken" | `GET /admin/sources/health` rolls the panel's own searches and downloads up into one verdict per source (working, flaky, failing, unproven), a success rate over the last handful of attempts, and the last error. Dependency health says whether Redis is up; this says whether one channel can still serve. |
| One broken channel no longer sinks the whole panel | The panel's own search prefers the channel it has observed answering when two channels tie, and a failed download refreshes the same query once and retries a single time on another channel's copy of the same recording. |
| The panel can read the service's own log | `GET /admin/logs` serves the most recent lines this process logged -- level, logger, message, exception text -- with a cursor and a level filter; URL query strings and credential-shaped fields are redacted before a line enters the window. |

## Architecture

```text
┌───────────────────────────────────┐    ┌───────────────────────────────────┐
│        WeCom callback app         │    │           Admin browser           │
└──────────┬────────────────────────┘    └──────────┬────────────────────────┘
           │                                        │
           HTTPS                                    HTTPS
           ▼                                        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                reverse proxy   (deploy/nginx, deploy/caddy)                │
└───────────────────────────────────────────────────────┬────────────────────┘
       │                                                │
       │                                                │ /admin + /api/*
       │                                                ▼
       │                  ┌──────────────────────────────────────────────────┐
       │                  │       admin-panel   (uid 10001, read-only)       │
       │                  │      rewrites /api/* to /admin/* on the app      │
       │                  └─────────────────────────────┬────────────────────┘
       │                                                │
       │ /wecom (callback)                              │ http://musicdl:8000
       ▼                                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│            musicdl main service   (uid 10001, read-only rootfs)            │
│                                                                            │
│┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐│
││       FastAPI        │  │    MessageWorker     │  │      JobWorker       ││
││    /wecom  /admin    │  │    search + rank     │  │   resolve+publish    ││
└──────────┬─────────────────────────┬─────────────────────────┬─────────────┘
           │                         │                         │
           HTTP                      redis://                  HTTPS
           ▼                         ▼                         ▼
┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│    plugin-runner     │   │   Redis (external)   │   │  kw / wy / tx / kg   │
│   internal network   │   │  jobs, dedup, state  │   │ + media hosts (TLS)  │
└──────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

`compose.yaml` runs three application services and no Redis. The `musicdl` service owns the media, application-data, and Telegram-session volumes; `plugin-runner` is attached only to the internal `plugin-control` network and receives no secrets; `admin-panel` is built from `docker/web/Dockerfile`, runs non-root on a read-only root filesystem like the other two, publishes its own loopback port, and reaches the app through the `/api/*` rewrites the build bakes in. Redis stays external and is configured with `MUSICDL_REDIS__URL`.

## Usage Example

A message that reaches the WeCom callback is parsed into one command. Bare text is treated as a query, so `月光` and `/search 月光` are the same request.

| Message | What the boundary does |
|---|---|
| `/search <query>` or bare text | Enqueues a search. The message worker queries the enabled sources, ranks the results, and replies with a numbered list. |
| `1` to `100` | Enqueues a selection for that candidate. The job worker resolves the media descriptor, streams and verifies the bytes, and publishes the file. |
| `n`, `p`, `/c`, `/cancel` | `parse_command` recognises them, but no worker consumes them yet, so they currently produce no reply. |
| Any other `/command` | Recorded as unsupported. |

The reply renders one line per candidate, with optional album, duration, and bitrate appended when the source states them:

```text
1. <title> - <artist>（<source_id>@<source_version>；格式 <format>；大小 <size>）

回复序号下载。
```

The `musicdl` service sends that text and the user answers with `1`. The selected candidate is bound to a generation, so a token minted against an older candidate list cannot select a refreshed one.

### Exercising a source without WeCom

The panel runs the same registry the workers do, so a source can be proved before any WeCom or Telegram account exists. Reach it over HTTPS, because the cookies are `Secure`, and reuse the cookie jar together with the `csrf_token` the login answered with:

```bash
curl -s -c cookies.txt -H 'content-type: application/json' \
  -d '{"username":"admin","password":"password"}' \
  https://<panel-host>/admin/login

curl -s -b cookies.txt 'https://<panel-host>/admin/search?q=<query>'

curl -s -b cookies.txt -H "x-csrf-token: <csrf_token>" -H 'content-type: application/json' \
  -d '{"candidate": <one candidate from the search reply>}' \
  https://<panel-host>/admin/download
```

The login answers `{"ok":true,"must_change":true,"csrf_token":"…"}`, the search answers the candidates plus one status per source, and the download answers the relative path, byte size, media type, and SHA-256. `GET /admin/media/<relative_path>` then serves that artifact back, so a browser can play what it fetched.

## Quick Install

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
cp .env.example .env
# Set MUSICDL_REDIS__URL in .env to your own external Redis endpoint.
docker compose build
docker compose up -d
```

Prerequisites:

- Docker Engine with the Compose plugin on a Debian-like Linux host.
- An externally managed Redis reachable from the `musicdl` container. Compose never starts one.
- Python 3.12 and a local virtual environment only if you intend to run the test suite.

The GitHub repository is private, so cloning requires access to `Gaoshou101/musicdl`.

## Quick Start

```bash
docker compose ps
curl http://127.0.0.1:${MUSICDL_PORT:-8000}/healthz
curl http://127.0.0.1:${MUSICDL_ADMIN_PORT:-3000}/healthz
```

`/healthz` answers `{"service":"musicdl","status":"ok","config_version":1}` and deliberately contacts neither Redis nor the plugin runner, so it proves the process is up and nothing more. Readiness is separate: with WeCom enabled `/readyz` pings Redis-backed state, and with it disabled there are no workers to lose, but `/readyz` still answers `503` if the source runtime the panel searches through could not be assembled.

The panel answers `{"status":"ok"}` on its own `/healthz`, the one route it serves without a session and the exact route its Compose health check probes. It reports that the panel process is serving requests and touches nothing behind it.

Three more steps take the deployment from running to useful:

1. Reach `/admin/` through an HTTPS reverse proxy, or through a loopback tunnel where browsers treat the host as trustworthy. Every cookie is marked `Secure`, so the panel is unreachable over plain HTTP. An unauthenticated visit is answered with the login page itself, and until the default credentials change every route except `/admin/`, `/admin/change-credentials`, and `/admin/change-credentials-form` answers `403`.
2. Install a source plugin with `POST /admin/sources`, or define a Telegram Bot with `POST /admin/bots`, and then search and download from the panel to prove the source answers. A deployment with no enabled source has nothing to search.
3. Enable the WeCom boundary with `MUSICDL_WECOM__ENABLED=true` and the credentials listed below, then send `/search <query>` from WeCom. The boundary is optional: the panel works without it.

## Configuration

All settings use the `MUSICDL_` prefix with `__` as the nesting delimiter. Copy `.env.example` to `.env` and replace only the placeholders; never commit `.env` or real credentials.

Part of the table below can also be moved into the panel: it keeps its own layer of overrides in the state file and applies them over the deployment's variables at startup. The panel only knows the fields it declares, and each one carries where its value came from (panel override, deployment variable, or default) and when it takes effect (immediately, or on the next start). The settings only Compose can change — published ports, volume mounts, resource limits — are listed separately with the variable to set, instead of being offered as a control that would lie.

| Variable | Required | Purpose |
|---|---|---|
| `MUSICDL_REDIS__URL` | Yes | External Redis endpoint; `redis://` or `rediss://`. |
| `MUSICDL_PORT` | No | Loopback host port published by Compose. Defaults to `8000`. |
| `MUSICDL_ADMIN_PORT` | No | Loopback host port Compose publishes for the panel. Defaults to `3000`. |
| `MUSICDL_WECOM__ENABLED` | To serve users | Turns on the callback boundary. Defaults to `false`. |
| `MUSICDL_WECOM__CORP_ID`, `__AGENT_ID`, `__TOKEN`, `__SECRET` | When WeCom is enabled | Corporation ID, agent ID, callback token, and application secret. |
| `MUSICDL_WECOM__ENCODING_AES_KEY` | When WeCom is enabled | The 43-character EncodingAESKey; it must decode to 32 bytes. |
| `MUSICDL_WECOM__ALLOWED_USERS` | When WeCom is enabled | The allowlist. Startup fails on an empty list. |
| `MUSICDL_ADMIN__ENABLED` | No | Mounts `/admin`. Defaults to `true`. |
| `MUSICDL_ADMIN__STATE_PATH` | No | Absolute POSIX path of the portal state file. Compose sets `/data/app/admin-state.json`. |
| `MUSICDL_ADMIN__LOGIN_LIMIT`, `__LOGIN_WINDOW_SECONDS` | No | Failed-login budget per window. Defaults to `5` per `60` seconds. |
| `MUSICDL_PLUGIN__SERVICE_URL` | Compose-set | Internal endpoint of the runner, `http://plugin-runner:8080`. |
| `MUSICDL_PLUGIN__APP_DATA_ROOT` | No | Where installed plugin code is stored. Defaults to `/data/app`. |
| `MUSICDL_MEDIA__ROOT` | Compose-set | Media mount for the main service, `/data/music`. |
| `MUSICDL_TELEGRAM__ENABLED` | No | Registers the Telegram connector and the Bot definitions the portal owns. Defaults to `false`. |
| `MUSICDL_TELEGRAM__API_ID`, `__API_HASH` | When Telegram is enabled | Telegram application credentials. |
| `MUSICDL_TELEGRAM__PROFILE`, `__SESSION_ROOT` | No | Restricted session profile and mount. Default to `default` and `/data/telegram-sessions`. |
| `MUSICDL_AI__ENABLED` | No | Advisory ranking and language classification. Defaults to `false`. |
| `MUSICDL_AI__BASE_URL`, `__API_KEY`, `__MODEL`, `__TIMEOUT`, `__MAX_CANDIDATES` | When AI is enabled | OpenAI-compatible endpoint settings. |
| `MUSICDL_WORKER__*` | No | Search, resolve, health, and job budgets. The settings validate their own inequalities at startup rather than at first use. |

## Administration Portal

The panel lives at `/admin` and works on its own: the registry it searches through is assembled whenever either the WeCom workers or the panel is enabled, so a deployment with no WeCom account still gets a working registry, needing neither Redis nor a WeCom client for it.

| Route | Purpose |
|---|---|
| `GET /admin/` | The dashboard. A browser with no session is answered with the login page itself. |
| `POST /admin/login-form` | The login page's own post: one page in, one redirect out, sharing the rate limit and the audit record with the JSON login. |
| `POST /admin/login` | Rate-limited JSON login that issues the session and CSRF cookies and returns the token. |
| `POST /admin/change-credentials-form` | The dashboard form that replaces the username and password while the defaults are still in force. |
| `POST /admin/change-credentials` | The same change as JSON. |
| `GET /admin/search?q=` | Runs the same `search_sources` the workers run and answers the candidates plus one status per source. |
| `POST /admin/download` | Downloads one candidate the search returned into the media root, answering the relative path, byte size, media type, and SHA-256. |
| `GET /admin/media/{path}` | Serves a downloaded artifact so a browser can play it, refusing any path that does not resolve below the media root. |
| `GET /admin/sources`, `POST /admin/sources` | List and install source plugins. |
| `POST /admin/sources/analyze` | Previews one import and stores nothing. |
| `PATCH /admin/sources/{id}`, `DELETE /admin/sources/{id}` | Enable, reprioritise, or remove a source. |
| `GET /admin/bots`, `POST /admin/bots`, `PATCH /admin/bots/{id}`, `DELETE /admin/bots/{id}` | Define and edit the Telegram Bots that become search sources. |
| `GET /admin/health` | Aggregated dependency health. |
| `GET /admin/config`, `PATCH /admin/config` | Read every setting the panel owns with its source and the value in force (a secret only answers whether one is set), then validate and store one batch of changes. |
| `GET /admin/sources/health` | Per-source roll-up of the last searches and downloads, with the stage and code of the last failure. |
| `GET /admin/events`, `GET /admin/audit` | Paginated event and audit logs. |
| `GET /admin/logs` | The in-memory window onto this process's own log records: cursor (`after`) paging and a `level` filter, for "what did the service print" rather than "what did the panel ask". |

The JSON routes keep answering JSON, so an unauthenticated `GET /admin/sources` is still a `401`, while the two HTML forms double-submit the session's CSRF token in a hidden field, because a form post cannot set the `x-csrf-token` header the API routes use. Form bodies are parsed in-process (`musicdl.admin.forms`) rather than through `request.form()`, so reading two strings does not make `python-multipart` a runtime dependency.

`GET /admin/health` reports four checks — `readyz`, `redis`, `plugin_runner`, and `telegram` — and every one of them answers `ok`, `failed`, or `not_required`. Redis is `not_required` without a WeCom account, and the plugin runner is `not_required` before a runtime needs it, so neither keeps a working deployment red. Telegram readiness is reported here rather than through `/readyz`: an enabled deployment shows `failed` unless a connector is wired, at least one Bot definition is registered, and the session restores as ready, while a disabled one shows `not_required`.

Channel health probes nothing of its own; it rolls up the panel's own searches and downloads. One search from the panel's search page gives every enabled source a row, and a download adds the question a search cannot answer, which is whether the channel can actually hand the audio back. The verdict comes from the last 20 outcomes per source: all successful is `ok`, a failed last attempt is `failing`, a failure followed by a success is `degraded`, and a source nobody has exercised is `unknown` — no evidence is not the same as broken. A source that has traffic but is no longer configured is still listed and marked as removed, because right after a source is deleted is exactly when its last error is worth reading, and the record is a fixed window per source rather than a growing log.

Mutating routes need the double-submit CSRF token. State is written to `MUSICDL_ADMIN__STATE_PATH` and reloaded on start, so an edited source, an edited Bot, and a changed password survive a restart, and a stored entry takes precedence over the value the runtime publishes again on boot. A state file that is unreadable or carries an unsupported version stops startup instead of silently restoring the default password, and a failed write rolls the in-memory change back.

That same state file carries the panel's own layer of settings under a `settings` key: a field the panel changed is recorded there, a field it did not is still decided by the deployment's environment, and only a field neither layer sets falls back to a default. A batch is validated and written as a whole, so one illegal value refuses the whole batch rather than storing half of it; a stored value this build can no longer accept is dropped at startup and listed on the page. Secrets never come back out — leaving one blank keeps the stored value, and only an explicit `null` clears the override and lets the deployment's value stand again.

## Web Panel

`web/` holds an optional Next.js dashboard that talks only to the routes above, with pages for the overview, sources, Bots, the running configuration, health, logs, and credentials. It rewrites its own `/api/*` calls to the app's `/admin/*` routes, and it fixes the app's origin while it builds rather than when it runs. The configuration page shows the fields the panel owns next to the container settings only Compose owns, and saves the whole batch of edits at once.

```bash
cd web
npm install
MUSICDL_API_ORIGIN=http://127.0.0.1:8000 npm run build
```

`npm run check:api:remote` runs a read-only contract check against a deployed panel, and `npm run check:api:local` runs the full flow against a local app and panel. The panel is one of the Compose services: `docker/web/Dockerfile` builds it, and Compose publishes `127.0.0.1:${MUSICDL_ADMIN_PORT:-3000}`. `web/STRUCTURE.md` maps the directory.

## Source Plugins

A manifest declares the language, operations, source digest, and the hosts the source may reach:

```json
{"plugin_id":"example","version":"1","language":"python",
 "operations":["search","resolve"],"allowed_hosts":["api.example.com"],
 "sha256":"<64 lowercase hex characters>"}
```

Four per-source grants are operator decisions rather than something a script can assert about itself: `allowed_ports`, `allow_insecure_http`, `allow_ip_hosts`, and `allow_any_host`. The last is the widest and the least protective. Every granted widening is reported back in `plugin.egress`, so an operator can read exactly what is in force before and after an install.

The supported entry point is a `handle(request)` function. It may return a JSON-compatible result or one HTTP `GET`/`POST` action at a time; an action may name headers and, for `POST`, a base64 body, but never the request line, `Host`, or a framing header.

Bounds are fixed rather than configurable per plugin:

| Surface | Limit |
|---|---|
| Plugin source | 256 KiB |
| Payload and result | 64 KiB |
| Brokered HTTP observation | 1 MiB |
| HTTP actions per job | 8 |
| Published media file | 500 MiB |
| Job wall time, CPU time | 30 s, 5 s |
| Python address space, Deno heap | 256 MiB, 128 MiB |

The main process owns the transfer. `SecureMediaTransport` normalises the host through IDNA, resolves A and AAAA records itself, rejects every non-global address, and then connects to the pinned numeric address while keeping the approved hostname for TLS SNI, certificate verification, and the `Host` header. It sends no ambient credentials, ignores proxy settings, never follows redirects, and closes the socket.

Some incompatibilities are deliberate: a plugin cannot import arbitrary packages, read environment variables or secrets, persist files, use arbitrary URLs, redirects or proxies, put credentials in a URL, or call the main service directly.

## Release Gates

`scripts/release/run_gates.py` decides the gates. `--gate all` is the default and treats `NOT_RUN` as a failure, so a gate that cannot run is never reported as passed. `--dry-run` prints the same table without failing the gates that need a deployment, and `--self-test` first tampers with the Compose, Telegram, proxy, and plugin boundaries and then prints the table.

| Gate | What it decides |
|---|---|
| `compose` | Three application services, no Redis service, non-root users, read-only root filesystems, hard CPU and memory limits, health probes, and an internal plugin network. |
| `proxy` | The Nginx and Caddy templates terminate TLS, keep the callback query string, and keep the callback out of their access logs. |
| `wecom-callback` | A real WeCom application behind public HTTPS. Human-run, with `--wecom-url` and `--wecom-expected`. |
| `telegram-session-isolation` | The session volume and its credentials belong to the main service only. |
| `media-integrity` | The media test files pass under `-W error`. |
| `plugin-security` | The static boundary holds, and the mandatory hostile runtime suite `tests/integration/test_plugin_runtime.py` passes with no skips against a real Docker Engine. |
| `admin-auth` | The admin test files pass under `-W error`. |
| `compose-recovery` | The restart and volume contract holds, the backup script resolves every volume, and an isolated Redis recovery drill passes. |

CI (`.github/workflows/ci.yml`) runs the test suite and `--self-test --dry-run` on every push and pull request. The hosted runner provides a Docker Engine, so `plugin-security` runs for real there; `wecom-callback` and `compose-recovery` stay human-run because no hosted runner can decide them honestly. A separate `admin-panel` job type-checks the dashboard, builds its image, and starts the container read-only to smoke-test `/healthz`, so the panel is proven in CI rather than only on an operator's machine.

## Development

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q -W error
```

The suite is 57 unit files plus 3 integration files. Two decide for themselves whether they can run: `tests/unit/test_worker_redis_live.py` skips unless `MUSICDL_TEST_REDIS_URL` is set, and `tests/integration/test_plugin_runtime.py` skips when no Docker Engine is available. A Docker-capable host with a test Redis is the only place that gives the complete answer.

The dashboard in `web/` has its own toolchain: `npm run build` compiles it, `npm run check:api:remote` checks the contract against a deployed panel, and `npm run check:api:local` runs the full flow against a local app and panel.

## Documentation

| Topic | What it covers | Link |
|---|---|---|
| Deployment environment | The external Redis URL Compose requires, plus the optional AI block | [.env.example](./.env.example) |
| Runtime configuration | Every configuration group and its default, declared in one model | [config.py](./src/musicdl/config.py) |
| Deployment shape | Services, volumes, networks, and security options | [compose.yaml](./compose.yaml) |
| Production limits | CPU, memory, and restart policy for every service | [compose.prod.yaml](./compose.prod.yaml) |
| Reverse proxies | Nginx and Caddy templates that terminate TLS in front of the portal | [deploy](./deploy) |
| Admin panel image | How the dashboard is built, where its backend origin is fixed, and how it runs as a container | [docker/web/Dockerfile](./docker/web/Dockerfile) |
| Web panel layout | The dashboard's directory map and its contract checks | [web/STRUCTURE.md](./web/STRUCTURE.md) |
| Release gates | The gate runner, its tamper self-test, and the backup drill | [run_gates.py](./scripts/release/run_gates.py) |
| Dependency inventory | Pinned versions, licences, and supply-chain evidence | [THIRD_PARTY.md](./THIRD_PARTY.md) |

No licence file is present in this repository, so this README makes no licence claim. Add a `LICENSE` file before publishing or redistributing the code.

