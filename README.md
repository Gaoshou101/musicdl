# musicdl

## Current implementation

The repository currently contains the Phase 0 foundation, the WeCom callback boundary, deterministic multi-source search, the Phase 3 Telegram user-account connector and its music-Bot adapters now wired into the runtime from the administration portal's Bot definitions, the Phase 4 injected download/media-safety engine, Phase 5 optional advisory ranking and language suggestions, the Phase 6 restricted plugin runtime with main-process streaming for plugin-resolved media, and the injected administration portal. AI is disabled by default and uses deterministic, redacted fallback behavior. Compatibility with a live provider remains deployment validation; no live provider call is claimed here. Concrete provider adapters remain integration work or later phases; the administration portal is mounted at `/admin` by default and can be switched off with `MUSICDL_ADMIN__ENABLED=false`.

`compose.yaml` defines only `musicdl` and `plugin-runner`; Redis remains an external service. Copy `.env.example` to `.env` and set `MUSICDL_REDIS__URL` before starting Compose. Never put credentials in the example file or source tree.

The main service owns the media, application-data, and Telegram-session volumes. The plugin runner receives no Redis URL, session, environment file, API key, main configuration, Docker socket, or host path.

The `/healthz` liveness handler does not contact Redis, the plugin runner, or other external dependencies. When WeCom is enabled, `/readyz` pings Redis-backed state to verify readiness; when it is disabled there are no workers to lose, but `/readyz` still answers 503 if the source runtime the panel searches through could not be assembled. The portal's dependency health reports four checks and each has three outcomes, not two: `ok`, `failed`, and `not_required` for a dependency that is real but has nothing to do in this deployment. Redis is `not_required` without a WeCom account, and the plugin runner is `not_required` before a runtime exists, so neither turns the card red on a deployment that is working. Telegram readiness is reported here rather than through `/readyz`: an enabled deployment shows `failed` unless a connector is wired, at least one Bot definition is registered, and the session restores as ready, while a disabled one shows `not_required`. The container images nevertheless install the Python runtime dependencies required by FastAPI and the application.

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
by HTTP actions handled by the main service's broker; the runner itself has no egress.
The broker applies the source's egress policy, and the policy narrows back to
`https` on port 443 to an exact host list unless the manifest explicitly grants more.

A manifest names the language, operations, SHA-256 source digest, and exact allowed
hosts, for example:

```json
{"plugin_id":"example","version":"1","language":"python",
 "operations":["search"],"allowed_hosts":["api.example.com"],
 "sha256":"<64 lowercase hex characters>"}
```

A manifest may also carry four per-source grants, each of which is an operator
decision rather than something a script can assert about itself:

- `allowed_ports` lists explicitly stated ports; the default port of an allowed
  scheme comes with that scheme.
- `allow_insecure_http` permits `http` in addition to `https`.
- `allow_ip_hosts` permits an address literal in place of a DNS name.
- `allow_any_host` drops the host allowlist for that one source.

`allow_any_host` is the widest and the least protective: a source that needs it
can be pointed at any host the deployment can reach, so it is only reasonable for a
script whose endpoints cannot be derived by reading it, and the analysis requires an
explicit grant for it. `POST /admin/sources` reports every granted widening back in
its `plugin.egress`, so an operator can see exactly what is in force.

An import can be reviewed before it happens. `POST /admin/sources/analyze` takes the
same body and answers with the analysis, the grants the script still needs, the id the
store would accept, and whether an install carrying the grants in that request would
succeed; it opens no stored script and writes none, so an operator can look before
anything is kept. Both routes are the interface a full administration console is
written against -- the portal's HTML is still a placeholder.

The supported entry point is a `handle(request)` function. A plugin may return a
JSON-compatible result or one HTTP `GET`/`POST` action at a time; the main action loop
supplies bounded observations. An action may name headers and, for `POST`, a base64
body, but never the request line, `Host`, or any framing header. Source is limited to
256 KiB, payload/result to 64 KiB, invocations to 16 MiB, and at most eight HTTP
actions/observations. Jobs are limited to 30 seconds wall time, 5 seconds CPU,
256 MiB Python address space (128 MiB Deno heap), 1 MiB file size, 32 file
descriptors, 64 KiB stdout, and 16 KiB stderr.

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

A `resolve` result is a five-field descriptor: the selected `candidate_id`, an absolute
`http` or `https` `url`, an `extension`, a `media_type`, and an optional
`declared_size`. The descriptor must match the media type implied by its extension and
stay bound to the `item_id` of the candidate the user confirmed; credentials, a
fragment, an out-of-range port, or a mismatched pair is rejected. Media bytes never
travel through plugin stdout.

The main process owns the transfer. `SecureMediaTransport` normalizes the host through
IDNA, applies the manifest's whole egress policy to the URL, resolves all A/AAAA
answers itself, rejects every non-global address, then connects to the pinned numeric
address while keeping the approved hostname for TLS SNI, certificate verification, and
the `Host` header. A plain-HTTP URL is sent unnegotiated, and only when that source
carries the grant. It sends no ambient credentials, ignores proxy settings, never
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
recorded as published. The category directory is decided before the reservation
from script evidence in the candidate metadata: kana or hangul gives `日韩`, han
characters give `华语`, and Latin letters give `欧美`, with the title outranking the
rest of the metadata. Anything else, including a candidate whose metadata carries
no recognizable script, stays `未知`; only those four values are ever written, and
a directory already fixed by a prepared reservation is reused instead of being
recomputed. With `MUSICDL_AI__ENABLED=true` the advisory classifier may override
the verdict with one of the four values, and a disabled, failing, or unusable
advisor leaves the deterministic verdict in place. The job is acknowledged (XACK)
only after the recorded effect for that stage has completed.

## Configuration

Copy `.env.example` to `.env` and replace only placeholders with deployment values. Do not commit `.env` or real credentials.

| Variable | Required | Description |
| --- | --- | --- |
| `MUSICDL_REDIS__URL` | Yes | External Redis URL; supports `redis://` and `rediss://`. |
| `MUSICDL_PORT` | No | Host loopback port; defaults to `8000`. |
| `MUSICDL_CONFIG__VERSION` | Compose-set | Configuration schema version, currently `1`. |
| `MUSICDL_MEDIA__ROOT` | Compose-set | Main media mount, `/data/music`. |
| `MUSICDL_TELEGRAM__SESSION_ROOT` | Compose-set | Restricted Telegram session mount, `/data/telegram-sessions`. |
| `MUSICDL_TELEGRAM__ENABLED` | No | Enables the Telegram user-account connector and the Bot definitions the portal owns; defaults to `false`. |
| `MUSICDL_TELEGRAM__API_ID` | When enabled | Telegram application API ID. |
| `MUSICDL_TELEGRAM__API_HASH` | When enabled | Telegram application API hash; never commit it. |
| `MUSICDL_TELEGRAM__PROFILE` | No | Restricted session profile name; defaults to `default`. |
| `MUSICDL_PLUGIN__SERVICE_URL` | Compose-set | Internal plugin endpoint, `http://plugin-runner:8080`. |
| `MUSICDL_ADMIN__ENABLED` | No | Mounts the `/admin` administration portal; defaults to `true`. |
| `MUSICDL_ADMIN__LOGIN_LIMIT` | No | Failed admin logins allowed per window before lockout; defaults to `5`. |
| `MUSICDL_ADMIN__LOGIN_WINDOW_SECONDS` | No | Length of the admin login rate-limit window in seconds; defaults to `60`. |
| `MUSICDL_ADMIN__STATE_PATH` | Compose-set | Absolute POSIX path of the administrator state file; unset disables persistence. |

The connector supports code login, 2FA, restart restoration, invalid-session and rate-limit states. `PublicTelegramBot` supplies the built-in `/search {query}` command contract, while `CustomTelegramBot` accepts a validated command template. An enabled Telegram deployment is wired into the running service at startup: every enabled Bot definition becomes one search source beside the plugin sources, and it is the configured command that the user account actually sends. Raw Bot responses are decoded at the connector boundary and normalized into the shared candidate model by `musicdl.telegram.decoder.decode_media_message`, which covers the contract where the Bot answers a search command by sending the audio file itself; a Bot that replies with a text list of results is a different contract and needs its own decoder, which is not implemented here. Real account credentials and the Bot definitions themselves still have to be supplied and validated in the deployment environment.

`/admin` serves the login page, the source and Bot controls, aggregated dependency health, and the event and audit logs. A browser that reaches `/admin/` with no session is answered with the login page itself, which posts to `POST /admin/login-form`, and while the default `admin`/`password` credentials are still in force that same dashboard carries the form that replaces them (`POST /admin/change-credentials-form`); the JSON routes keep answering JSON, so an unauthenticated `GET /admin/sources` is still a 401. Both forms double-submit the session's CSRF token in a hidden field, because a form post cannot set the `x-csrf-token` header the API routes use, and both share the login rate limit and the audit record with `POST /admin/login`. Form bodies are parsed in-process (`musicdl.admin.forms`) rather than through `request.form()`, so reading two strings does not make `python-multipart` a runtime dependency. The session and CSRF cookies are marked `Secure`, so the panel must be reached over HTTPS through a TLS-terminating reverse proxy, or over a loopback tunnel where browsers treat the host as trustworthy. Logins are rate limited, mutating routes require the double-submit CSRF token, and until the default `admin`/`password` credentials are changed every panel route except `/admin/`, `/admin/change-credentials` and `/admin/change-credentials-form` answers 403. The portal lists the sources the runtime registry assembled at startup.

The panel searches and downloads on its own, which is how a music source is exercised without sending the bot a message. `GET /admin/search?q=` runs the same `search_sources` over the same registry the workers use and answers with the candidates plus one status per source; `POST /admin/download` takes one candidate the search returned and downloads it into the media root, answering with the relative path, byte size, media type and SHA-256; and `GET /admin/media/{path}` serves that artifact back so a browser can play what it fetched, refusing any path that does not resolve below the media root. All three require a session, and the download requires the CSRF token. None of this is a WeCom feature: the source registry is assembled whenever either the WeCom workers or the panel is enabled, so a deployment with no WeCom account still gets a working registry, needing neither Redis nor a WeCom client for it. A runtime that cannot be assembled at start-up leaves the panel serving pages and `/readyz` answering 503 rather than reporting a search that would succeed.

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

## Release gates

`scripts/release/run_gates.py` decides the release gates. `--gate all` is the
default and treats NOT_RUN as failure, so a gate that cannot run is never
reported as passed; `--dry-run` prints the same table without failing on the
gates that need this deployment, and `--self-test` first tampers with the
Compose, Telegram, proxy, and plugin boundaries and then prints the table.

The callback gate needs a real WeCom application behind public HTTPS
(`--wecom-url` with `--wecom-expected`) and the recovery gate needs the
deployment Redis (`--redis-url` with `--confirm-isolated`), so both stay
human-run. CI (`.github/workflows/ci.yml`) runs the test suite and
`--self-test --dry-run` on every push and pull request; the plugin-security gate
runs for real there because hosted runners provide a Docker Engine, and it
reports NOT_RUN rather than PASS on a host that has none.

## Verification

Run the strict local test suite with:

```bash
python -m pytest -q -W error
```

The health tests use the ASGI application directly and the Compose tests parse `compose.yaml` with PyYAML. Runtime Docker verification must be performed on a Docker-capable deployment host.
