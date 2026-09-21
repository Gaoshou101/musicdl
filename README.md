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
| The running configuration is editable from the panel | `GET /admin/config` and `PATCH /admin/config` move the WeCom, Redis, Telegram, AI, and worker budgets out of the host's `.env` and into the panel. Every setting the panel owns is adopted in place: the save rebuilds the runtime from the settings in force, replaces its workers, and reports the rebuild back to the page, so a changed credential, budget, or Bot does not cost a restart. Secrets are write-only, and the settings only Compose owns say so. |
| Channel health answers "which source is broken" | `GET /admin/sources/health` rolls the panel's own searches and downloads up into one verdict per source (working, flaky, failing, unproven), a success rate over the last handful of attempts, and the last error. Dependency health says whether Redis is up; this says whether one channel can still serve. |
| One broken channel no longer sinks the whole panel | The panel's own search prefers the channel it has observed answering when two channels tie, and a failed download refreshes the same query once and retries a single time on another channel's copy of the same recording. |
| The panel can read the service's own log | `GET /admin/logs` serves the most recent lines this process logged -- level, logger, message, exception text -- with a cursor and a level filter; URL query strings and credential-shaped fields are redacted before a line enters the window. |
| A Telegram Bot is a source, whichever way it answers | A Bot that sends the audio file and a Bot that answers with a numbered list whose inline buttons fetch one entry are both served: the listing is the search result, the button press is the download, and the query is recorded in the candidate so the same listing is replayed rather than guessed at. A Bot streams at whatever the account's link to Telegram allows, so such a channel names its own stream budget (`stream_budget_seconds`) instead of being cut off at the CDN-sized one. |

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
                                                       │  /wecom/callback, the console, and /admin
                                                       ▼  are one port: http://musicdl:8000
┌────────────────────────────────────────────────────────────────────────────┐
│            musicdl main service   (uid 10001, read-only rootfs)            │
│                                                                            │
│┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐│
││       FastAPI        │  │    MessageWorker     │  │      JobWorker       ││
││  /wecom  /admin  /   │  │    search + rank     │  │   resolve+publish    ││
││  + static console    │  │                      │  │                      ││
└──────────┬─────────────────────────┬─────────────────────────┬─────────────┘
           │                         │                         │
           HTTP                      redis://                  HTTPS
           ▼                         ▼                         ▼
┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│    plugin-runner     │   │   Redis (external)   │   │  kw / wy / tx / kg   │
│   internal network   │   │  jobs, dedup, state  │   │ + media hosts (TLS)  │
└──────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

`compose.yaml` runs two application services and no Redis. The `musicdl` service owns the media, application-data, and Telegram-session volumes and serves the console from `/`, which `docker/main/Dockerfile` builds into its own image; `plugin-runner` is attached only to the internal `plugin-control` network and receives no secrets. The console is static files rather than a second runtime: production runs no Node process for it, publishes no second port, and needs no rewrite whose paths could drift from the API's. Redis stays external and is configured with `MUSICDL_REDIS__URL`.

## Usage Example

A message that reaches the WeCom callback is parsed into one command. Bare text is treated as a query, so `月光` and `/search 月光` are the same request.

| Message | What the boundary does |
|---|---|
| `/search <query>` or bare text | Enqueues a search. The message worker queries the enabled sources, ranks the results, and replies with a numbered list. |
| `1` to `100` | Enqueues a selection for that candidate. The job worker resolves the media descriptor, streams and verifies the bytes, and publishes the file. |
| `n`, `p`, `/c`, `/cancel` | `parse_command` recognises them, but no worker consumes them yet, so they currently produce no reply. |
| Any other `/command` | Recorded as unsupported. |

The words are taken as typed, and the search folds the separator between a song and its artist, so `月光 陈慧娴` and `月光-陈慧娴` are one request. The reply says what was searched and then renders one short line per candidate:

```text
「月光-陈慧娴」找到 84 个结果：

1. 月光 — 陈慧娴《几时再见演唱会》  ·  4:29  ·  FLAC 1411kbps  ·  网易云
2. 月光 — 陈慧娴《永远是你的朋友》  ·  4:46  ·  MP3 320kbps  ·  QQ音乐

回复序号下载。
```

Every row leads with the title, the artist, and the album, and then appends only what the channel actually stated: the running time, the quality, and the catalogue named the way a person reads it. A missing field is left out rather than printed as `未知`. The `musicdl` service sends that text and the user answers with `1`. The selected candidate is bound to a generation, so a token minted against an older candidate list cannot select a refreshed one; when a download fails and the refreshed list is sent instead, that prompt says why it repeats.

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
```

One port serves the whole portal: `/` is the console, `/admin/*` is the API it calls, and `/wecom/callback` is the callback boundary. Everything above `/wecom` belongs to one process, so there is no second health endpoint to probe and no second runtime that could report itself healthy while the app was down.

`/healthz` answers `{"service":"musicdl","status":"ok","config_version":1}` and deliberately contacts neither Redis nor the plugin runner, so it proves the process is up and nothing more. Readiness is separate: with WeCom enabled `/readyz` pings Redis-backed state, and with it disabled there are no workers to lose, but `/readyz` still answers `503` if the source runtime the panel searches through could not be assembled.

Three more steps take the deployment from running to useful:

1. Reach `/admin/` through an HTTPS reverse proxy, or through a loopback tunnel where browsers treat the host as trustworthy. Every cookie is marked `Secure`, so the panel is unreachable over plain HTTP. An unauthenticated visit is answered with the login page itself, and until the default credentials change every route except `/admin/`, `/admin/change-credentials`, and `/admin/change-credentials-form` answers `403`.
2. Install a source plugin with `POST /admin/sources`, or define a Telegram Bot with `POST /admin/bots`, and then search and download from the panel to prove the source answers. A deployment with no enabled source has nothing to search. The image ships a `music_v1bot` definition (sent as `/search {query}`), so a fresh install already has one; rename, disable or delete it from the Bot page and the deletion sticks across restarts. `music_v1bot` answers with a numbered list and only sends the audio when its inline button is pressed, which is measured behaviour rather than a guess: the adapter decodes that listing into one candidate per button, records the query behind each entry, and replays the search on download before pressing the entry's own button. A Bot that answers with the file itself is served by the same adapter. A Telegram Bot needs an authorised account behind it: `api_id` and `api_hash` wire the client, and the Bot page's own login card finishes the one-time sign-in (phone number, the code Telegram sends, and the two-step password when the account has one), so the Bot page says `未登录` instead of every search failing with `invalid_session`.
3. Enable the WeCom boundary with `MUSICDL_WECOM__ENABLED=true` and the credentials listed below, then send `/search <query>` from WeCom. The boundary is optional: the panel works without it.

## Configuration

All settings use the `MUSICDL_` prefix with `__` as the nesting delimiter. Copy `.env.example` to `.env` and replace only the placeholders; never commit `.env` or real credentials.

Part of the table below can also be moved into the panel: it keeps its own layer of overrides in the state file and applies them over the deployment's variables at startup. The panel only knows the fields it declares, and each one carries where its value came from (panel override, deployment variable, or default). Every field it owns is adopted immediately — a save only reloads the runtime when a field the runtime actually reads has changed, and the reply says which generation the reload produced — so the settings only Compose can change (published ports, volume mounts, resource limits) are listed separately with the variable to set, instead of being offered as a control that would lie.

| Variable | Required | Purpose |
|---|---|---|
| `MUSICDL_REDIS__URL` | Yes | External Redis endpoint; `redis://` or `rediss://`. |
| `MUSICDL_PORT` | No | Loopback host port published by Compose. Defaults to `8000`. |
| `MUSICDL_ADMIN__PANEL_ROOT` | No | Where the console's static export lives inside the container. The app's image sets `/app/panel`; a checkout that never built the console serves no front page at `/` and logs a warning instead of refusing to start. |
| `MUSICDL_WECOM__ENABLED` | To serve users | Turns on the callback boundary. Defaults to `false`. |
| `MUSICDL_WECOM__CORP_ID`, `__AGENT_ID`, `__TOKEN`, `__SECRET` | When WeCom is enabled | Corporation ID, agent ID, callback token, and application secret. |
| `MUSICDL_WECOM__API_BASE` | Optional | Where outbound API calls go; defaults to the official `https://qyapi.weixin.qq.com`. A deployment outside mainland China can point it at a reverse proxy (for example `ddsderek/wxchat` on `http://host:9080`), so `gettoken` and message sends leave from a trusted address. Plain HTTP on a public link exposes the application secret, so prefer HTTPS where the proxy offers it. |
| `MUSICDL_WECOM__ENCODING_AES_KEY` | When WeCom is enabled | The 43-character EncodingAESKey; it must decode to 32 bytes. |
| `MUSICDL_WECOM__ALLOWED_USERS` | When WeCom is enabled | The allowlist. Startup fails on an empty list. |
| `MUSICDL_ADMIN__ENABLED` | No | Mounts `/admin`. Defaults to `true`. |
| `MUSICDL_ADMIN__STATE_PATH` | No | Absolute POSIX path of the portal state file. Compose sets `/data/app/admin-state.json`. |
| `MUSICDL_ADMIN__LOGIN_LIMIT`, `__LOGIN_WINDOW_SECONDS` | No | Failed-login budget per window. Defaults to `5` per `60` seconds. |
| `MUSICDL_PLUGIN__SERVICE_URL` | Compose-set | Internal endpoint of the runner, `http://plugin-runner:8080`. |
| `MUSICDL_PLUGIN__APP_DATA_ROOT` | No | Where installed plugin code is stored. Defaults to `/data/app`. |
| `MUSICDL_MEDIA__ROOT` | Compose-set | Media mount for the main service, `/data/music`. |
| `MUSICDL_TELEGRAM__ENABLED` | No | Registers the Telegram connector and the Bot definitions the portal owns. Defaults to `false`; with the connector off the built-in `music_v1bot` definition still shows up in the portal, it just registers no source. |
| `MUSICDL_TELEGRAM__API_ID`, `__API_HASH` | When Telegram is enabled | Telegram application credentials. |
| `MUSICDL_TELEGRAM__PROFILE`, `__SESSION_ROOT` | No | Restricted session profile and mount. Default to `default` and `/data/telegram-sessions`. |
| `MUSICDL_AI__ENABLED` | No | Advisory ranking and language classification. Defaults to `false`. |
| `MUSICDL_AI__BASE_URL`, `__API_KEY`, `__MODEL`, `__TIMEOUT`, `__MAX_CANDIDATES` | When AI is enabled | OpenAI-compatible endpoint settings. |
| `MUSICDL_AI__USER_AGENT` | No | Optional User-Agent for the completion request. Some relays only answer a specific client -- agentrouter.org rejected every default Python, curl and browser agent with `401 unauthorized_client_error` and admitted `claude-cli/1.0.0 (external, cli)`. Blank keeps the HTTP client's own value. |
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
| `GET /admin/bots`, `POST /admin/bots`, `PATCH /admin/bots/{id}`, `DELETE /admin/bots/{id}` | Define and edit the Telegram Bots that become search sources; a deployment that never stored a bot list starts from the built-in `music_v1bot`. |
| `GET /admin/telegram` | The stored Telegram session's own state (`ready`, `invalid_session`, `code_required`, `password_required`, `rate_limited`, `error`) and the masked number a half-finished login waits on. |
| `POST /admin/telegram/login`, `POST /admin/telegram/login/verify`, `POST /admin/telegram/login/password` | The one-time sign-in: send the code to a phone number, submit the code, then the two-step password if the account has one. |
| `POST /admin/telegram/logout` | Forget the stored session, so the next login starts from a fresh code. |
| `GET /admin/health` | Aggregated dependency health. |
| `GET /admin/config`, `PATCH /admin/config` | Read every setting the panel owns with its source and the value in force (a secret only answers whether one is set), then validate and store one batch of changes; the write is adopted in place and the reply carries the rebuild under `reload` (`reloaded`, `failed`, or `skipped`). |
| `GET /admin/sources/health` | Per-source roll-up of the last searches and downloads, with the stage and code of the last failure. |
| `GET /admin/events`, `GET /admin/audit` | Paginated event and audit logs. |
| `GET /admin/logs` | The in-memory window onto this process's own log records: cursor (`after`) paging and a `level` filter, for "what did the service print" rather than "what did the panel ask". |

The JSON routes keep answering JSON, so an unauthenticated `GET /admin/sources` is still a `401`, while the two HTML forms double-submit the session's CSRF token in a hidden field, because a form post cannot set the `x-csrf-token` header the API routes use. Form bodies are parsed in-process (`musicdl.admin.forms`) rather than through `request.form()`, so reading two strings does not make `python-multipart` a runtime dependency.

`GET /admin/health` reports four checks — `readyz`, `redis`, `plugin_runner`, and `telegram` — and every one of them answers `ok`, `failed`, or `not_required`. Redis is `not_required` without a WeCom account, and the plugin runner is `not_required` before a runtime needs it, so neither keeps a working deployment red. Telegram readiness is reported here rather than through `/readyz`: an enabled deployment shows `failed` unless a connector is wired, at least one Bot definition is registered, and the session restores as ready, while a disabled one shows `not_required`.

Channel health probes nothing of its own; it rolls up the panel's own searches and downloads. One search from the panel's search page gives every enabled source a row, and a download adds the question a search cannot answer, which is whether the channel can actually hand the audio back. The verdict comes from the last 20 outcomes per source: all successful is `ok`, a failed last attempt is `failing`, a failure followed by a success is `degraded`, and a source nobody has exercised is `unknown` — no evidence is not the same as broken. A source that has traffic but is no longer configured is still listed and marked as removed, because right after a source is deleted is exactly when its last error is worth reading, and the record is a fixed window per source rather than a growing log.

Mutating routes need the double-submit CSRF token. State is written to `MUSICDL_ADMIN__STATE_PATH` and reloaded on start, so an edited source, an edited Bot, and a changed password survive a restart, and a stored entry takes precedence over the value the runtime publishes again on boot. A state file that is unreadable or carries an unsupported version stops startup instead of silently restoring the default password, and a failed write rolls the in-memory change back.

That same state file carries the panel's own layer of settings under a `settings` key: a field the panel changed is recorded there, a field it did not is still decided by the deployment's environment, and only a field neither layer sets falls back to a default. A batch is validated and written as a whole, so one illegal value refuses the whole batch rather than storing half of it; a stored value this build can no longer accept is dropped at startup and listed on the page. Secrets never come back out — leaving one blank keeps the stored value, and only an explicit `null` clears the override and lets the deployment's value stand again.

Changing any of it does not need a restart. A save that moves a field the runtime reads — a WeCom credential or proxy, a Telegram Bot list, a Redis URL, a worker budget — rebuilds the runtime in place: the next runtime is assembled from the settings in force *before* the running one is touched, so a build that fails leaves the working deployment alone, the workers of the runtime that was replaced are cancelled and their new counterparts take over, and every mutating reply carries the outcome under `reload` (`reloaded` with the generation and channel count, `failed` with the reason, or nothing at all when the change touched neither). A cancelled worker leaves its message pending in its Redis stream, where the next runtime's `XAUTOCLAIM` finds it, so a reload costs a retry rather than a message: `docker ps` shows the same `Up` time across a credential change.

## Web Panel

`web/` holds an optional Next.js dashboard that talks only to the routes above, with pages for the overview, sources, Bots, the running configuration, health, logs, and credentials. It is client-only, so `next build` exports static files that the app serves from `/`, and it calls the app's `/admin/*` routes on that same origin -- there is no rewrite to maintain and no origin to fix at build time. The configuration page shows the fields the panel owns next to the container settings only Compose owns, and saves the whole batch of edits at once.

The Bot page also carries the Telegram account the definitions are called with: the session's own state, a first login in three steps (number, code, two-step password), and a sign-out that forgets the stored session. A Bot definition says which bot to ask; this card is what makes the asking possible.

```bash
cd web
npm install
npm run build
```

`npm run check:api:remote` runs a read-only contract check against a deployed console, and `npm run check:api:local` runs the full flow against a local app. The console is not a service of its own: `docker/main/Dockerfile` builds it into the app's image and the app serves it from `/`, so an app whose `MUSICDL_ADMIN__PANEL_ROOT` points at `web/out` is the whole setup. `web/STRUCTURE.md` maps the directory.

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

CI (`.github/workflows/ci.yml`) runs the test suite and `--self-test --dry-run` on every push and pull request. The hosted runner provides a Docker Engine, so `plugin-security` runs for real there; `wecom-callback` and `compose-recovery` stay human-run because no hosted runner can decide them honestly. A separate `admin-panel` job type-checks the dashboard, then builds and starts the app service through `compose.prod.yaml` itself -- the same read-only root filesystem, volumes, and command a host runs -- and asks it for `/healthz`, the console's front page, and a `401` from `/admin/sources`. Restating that boundary in the workflow would prove the flags the workflow wrote; driving Compose proves the container that ships.

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
| Reverse proxies | Nginx and Caddy templates that terminate TLS in front of the portal; every request reaches one upstream, because the console and the API behind it are the same process | [deploy](./deploy) |
| Console build | How the dashboard is compiled into the app's image and where the static export lands | [docker/main/Dockerfile](./docker/main/Dockerfile) |
| Web panel layout | The dashboard's directory map and its contract checks | [web/STRUCTURE.md](./web/STRUCTURE.md) |
| Release gates | The gate runner, its tamper self-test, and the backup drill | [run_gates.py](./scripts/release/run_gates.py) |
| Dependency inventory | Pinned versions, licences, and supply-chain evidence | [THIRD_PARTY.md](./THIRD_PARTY.md) |

No licence file is present in this repository, so this README makes no licence claim. Add a `LICENSE` file before publishing or redistributing the code.

