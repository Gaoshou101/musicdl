# musicdl

## Current implementation

The repository currently contains the Phase 0 foundation, the WeCom callback boundary, deterministic multi-source search, the Phase 3 Telegram user-account connector and music-Bot adapter contracts, the Phase 4 injected download/media-safety engine, Phase 5 optional advisory ranking and language suggestions, the Phase 6 restricted plugin runtime with main-process streaming for plugin-resolved media, and the injected administration portal. AI is disabled by default and uses deterministic, redacted fallback behavior. Compatibility with a live provider remains deployment validation; no live provider call is claimed here. Concrete provider adapters remain integration work or later phases; the administration portal is mounted at `/admin` by default and can be switched off with `MUSICDL_ADMIN__ENABLED=false`.

`compose.yaml` defines only `musicdl` and `plugin-runner`; Redis remains an external service. Copy `.env.example` to `.env` and set `MUSICDL_REDIS__URL` before starting Compose. Never put credentials in the example file or source tree.

The main service owns the media, application-data, and Telegram-session volumes. The plugin runner receives no Redis URL, session, environment file, API key, main configuration, Docker socket, or host path.

The `/healthz` liveness handler does not contact Redis, the plugin runner, or other external dependencies. When WeCom is enabled, `/readyz` pings Redis-backed state to verify readiness. The container images nevertheless install the Python runtime dependencies required by FastAPI and the application.

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

A plugin reports search results, and for a confirmed candidate it answers `resolve`
with a media descriptor instead of returning bytes. The manifest may still name a
`download` operation for compatibility, but the main service never invokes it and
always requires `resolve` for plugin media.

Incompatibilities are intentional: plugins cannot import arbitrary packages, access
environment variables or secrets, persist files, use arbitrary URLs/redirects/proxies,
use credentials in URLs, or call the main service directly. Docker runtime checks are
required for release; this Windows checkout can only report the static/unit evidence
when Docker Engine is unavailable.

## Plugin download boundary

A `resolve` result is a five-field descriptor: the selected `candidate_id`, an
implicit-port `https` `url`, an `extension`, a `media_type`, and an optional
`declared_size`. The descriptor must match the media type implied by its extension and
stay bound to the `item_id` of the candidate the user confirmed; credentials, an
explicit port, a fragment, or a mismatched pair is rejected. Media bytes never travel
through plugin stdout.

The main process owns the transfer. `SecureMediaTransport` normalizes the host through
IDNA, matches it against the manifest's exact `allowed_hosts`, resolves all A/AAAA
answers itself, rejects every non-global address, then connects to the pinned numeric
address while keeping the approved hostname for TLS SNI, certificate verification, and
the `Host` header. It sends no ambient credentials, ignores proxy settings, never
follows redirects, bounds the response, and closes the socket.
`PluginSource.download()` composes that typed `resolve` with that transport, so a
plugin-backed download never calls the manifest's compatibility `download` operation.

Bounds are fixed: plugin responses and results are limited to 64 KiB, one brokered
HTTP observation to 1 MiB, and a published media file to 500 MiB. The transport owns
its response closer, which runs exactly once on success, failure, and cancellation.

Worker budgets come from `AppSettings.worker`, and the settings validate their own
inequalities at startup: resolve/stream, refresh/search, and health budgets must fit
inside the job timeout with the configured slack still remaining, and the health
timeout must not exceed the job timeout.

Selection is generation-bound. A selection job carries the immutable candidate
snapshot and generation captured when it was created, so a token issued for an older
generation cannot select a refreshed candidate. Every side effect — the download, the
failure refresh, the health probe, the rebind, and each WeCom notice — is guarded by
its own fenced, durable job-effect marker, and a replanned or retried job never
repeats a side effect that already completed.

Notifications are separate effects: `success_notice`, `selection_prompt`, and
`terminal_failure_notice`. An acknowledged notification replays durably from its
record; an uncertain outcome is terminal and is never resent, so `prompt_uncertain`
stays uncertain instead of prompting the user again.

A failed download excludes exactly the failed source, health-checks that source once,
persists the actual refreshed candidates, and sends one new `selection_prompt` for the
user to choose from. It never downloads a replacement automatically.

Publication stays inside the Phase 4 path: the worker reserves a destination suffix
through the artifact ledger, streams while hashing, verifies the signature, extension,
and media type, and renames the temporary file into place before the artifact is
recorded as published. Language directories normalize to `华语`, `欧美`, `日韩`, or
`未知`, so a missing or unrecognized language is archived under `未知`. The job is
acknowledged (XACK) only after the recorded effect for that stage has completed.

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
| `MUSICDL_ADMIN__ENABLED` | No | Mounts the `/admin` administration portal; defaults to `true`. |
| `MUSICDL_ADMIN__LOGIN_LIMIT` | No | Failed admin logins allowed per window before lockout; defaults to `5`. |
| `MUSICDL_ADMIN__LOGIN_WINDOW_SECONDS` | No | Length of the admin login rate-limit window in seconds; defaults to `60`. |

The connector supports code login, 2FA, restart restoration, invalid-session and rate-limit states. `PublicTelegramBot` supplies the built-in `/search {query}` command contract, while `CustomTelegramBot` accepts a validated command template. Raw Bot responses are decoded at the connector boundary and normalized into the shared candidate model. Real account credentials, the chosen public Bot, and any custom Bot response decoder must be supplied and validated in the deployment environment.

`/admin` serves the login page, the source and Bot controls, aggregated dependency health, and the event and audit logs. The session and CSRF cookies are marked `Secure`, so the panel must be reached over HTTPS through a TLS-terminating reverse proxy. Logins are rate limited, mutating routes require the double-submit CSRF token, and until the default `admin`/`password` credentials are changed every panel route except `/admin/` and `/admin/change-credentials` answers 403. The portal lists the sources the runtime registry assembled at startup; source and Bot edits live in process memory and do not yet survive a restart.

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
