<h1 align="center">musicdl</h1>

<p align="center">A self-hosted, multi-source music download service with a Chinese admin panel, WeCom workflows, Telegram sources, and isolated JavaScript plugins.</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="./web/public/brand-options/social-preview-cool-aurora-glass.png" alt="Cool porcelain aurora glass artwork for musicdl" width="720">
</p>

<p align="center">
  <a href="https://github.com/Gaoshou101/musicdl/actions/workflows/ci.yml"><img src="https://github.com/Gaoshou101/musicdl/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://github.com/Gaoshou101/musicdl/releases"><img src="https://img.shields.io/github/v/release/Gaoshou101/musicdl?style=flat-square&color=F59E0B" alt="Latest release"></a>
  <a href="https://hub.docker.com/r/wit7zz/musicdl"><img src="https://img.shields.io/badge/Docker-amd64%20%7C%20arm64-4B5563?style=flat-square" alt="Docker architectures"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-22C55E?style=flat-square" alt="MIT License"></a>
</p>

## Highlights

| Highlight | Why it matters |
|---|---|
| Multi-source search and download | Search installed sources from one interface and retry another channel when a source fails. |
| Built-in administration panel | Manage sources, Telegram bots, runtime settings, health status, downloads, events, audits, and service logs in Simplified Chinese. |
| Importable JavaScript sources | Analyze and import compatible LX Music source scripts without baking third-party scripts into the image. |
| WeCom and Telegram integration | Use song names in WeCom conversations or register Telegram music bots as additional channels. |
| Isolated plugin execution | JavaScript plugins run in a separate non-root container with a read-only filesystem, no Linux capabilities, and bounded resources. |
| Reproducible deployment | Versioned Docker images are published for `linux/amd64` and `linux/arm64`. |

## Architecture

```text
┌────────────────┐  HTTPS  ┌──────────────────────────────┐
│ Browser / Admin│────────▶│        musicdl service       │
└────────────────┘         │  API, panel, search, workers │
                           └──────────────────────────────┘
┌────────────────┐                     │
│     WeCom      │◀──── callback ──────┤
└────────────────┘                     │
                                      ├──────────────▶┌──────────────┐
┌────────────────┐                     │  state/jobs   │    Redis     │
│    Telegram    │◀──── Telethon ──────┤               └──────────────┘
└────────────────┘                     │
                                      │ internal HTTP
                                      ▼
                           ┌──────────────────────────────┐
                           │   isolated plugin runner     │
                           │  Deno, seccomp, no secrets   │
                           └──────────────────────────────┘
                                      │
                                      ▼
                           ┌──────────────────────────────┐
                           │ Imported sources and music   │
                           │ provider endpoints           │
                           └──────────────────────────────┘
```

The administration panel is built into the main image. Only the plugin runner remains a separate container because it executes imported code.

### Which Compose file should I use?

The repository ships four manifests for four different situations. Each file's header states the same summary; pick with this table:

| Your situation | Manifest | Command |
|---|---|---|
| Try it out first (no checkout, bundled Redis) | `compose.quick.yaml` | `docker compose -f compose.quick.yaml up -d` |
| Already run Redis; deploy a pinned release image | `compose.prod.yaml` | `MUSICDL_IMAGE_TAG=1.0.2 docker compose -f compose.prod.yaml up -d` |
| Developing musicdl from this checkout | `compose.yaml` | `docker compose up -d --build` |
| No Redis available; main service + runner only | `compose.lite.yaml` | `docker compose -f compose.lite.yaml up -d --build` |

## Quick Install

Requirements:

- Docker Engine with the Docker Compose plugin
- Internet access to pull the published images

```bash
curl -fsSLo compose.quick.yaml https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.quick.yaml
docker compose -f compose.quick.yaml up -d
```

On Windows PowerShell, download the same single file with:

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.quick.yaml -OutFile compose.quick.yaml
docker compose -f .\compose.quick.yaml up -d
```

This quick path uses prebuilt images and includes a private Redis service. It does not need a repository checkout, `.env` file, or separately managed Redis. Redis has no published host port, stores its data in a Docker volume, and is reachable only from the main service. The plugin runner remains isolated on its internal control network and receives no application secrets. The web service binds to `127.0.0.1:8000` by default.

By default, administrator session and CSRF cookies require HTTPS (`MUSICDL_ADMIN__COOKIE_SECURE=true`); prefer an HTTPS reverse proxy. For ongoing direct HTTP access from the Docker host via `127.0.0.1` only, create or edit a `.env` file next to `compose.quick.yaml` and add:

```dotenv
MUSICDL_ADMIN__COOKIE_SECURE=false
```

Then recreate the main service:

```bash
docker compose -f compose.quick.yaml up -d --force-recreate musicdl
```

The quick-install default needs no `.env` file; if the setting is omitted, cookies remain HTTPS-only. Keeping the line in `.env` preserves the HTTP setting across later Compose recreations. To restore the secure default, remove the line and force-recreate `musicdl`. The quick manifest binds to loopback by default, so this does not expose the service to LAN peers; LAN access requires a separate intentional port-binding or proxy change.

A fresh quick install starts with username `admin` and password `password`. Immediately set a new, strong password; the panel blocks the rest of the administration API until the credential change is completed. Keep the service on loopback and private while the default credentials are active.

Check that the service is healthy after starting it with the intended cookie setting:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). HTTP sends credentials and session cookies in plaintext, so keep direct HTTP limited to trusted host-local access and use HTTPS for public access.

The quick Compose project name defaults to `musicdl`. To change the host port, set `MUSICDL_PORT` before running Compose. To select a different published application image, set `MUSICDL_IMAGE_TAG`; the quick manifest now defaults to `1.0.2`. Both application images use the same version tag.

## Lite Two-Container Deployment

The full three-service quick install above remains the default and includes private Redis plus WeCom support. Choose lite only when WeCom is not needed: lite runs the app and isolated plugin runner, starts no Redis service, and does not connect to Redis. The app rejects WeCom being enabled in this mode; AI and Telegram settings remain available. The manifest sets `MUSICDL_DEPLOYMENT_MODE=lite`. The published `1.0.2` main and plugin-runner image tags support lite. `compose.lite.yaml` still builds both images from the source checkout with `docker/main/Dockerfile` and `docker/plugin/Dockerfile`; follow its build step before starting lite. The app fails closed before Uvicorn starts if the image does not declare and activate lite mode.

Clone the repository and prepare a private working directory:

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
cp .env.example .env
```

Set any needed deployment values in `.env` (for example `MUSICDL_PORT` or `MUSICDL_ADMIN__COOKIE_SECURE`). If you copied `.env.example`, remove or comment out its `MUSICDL_REDIS__URL` line; lite does not pass that setting to the app, start Redis, or connect to Redis. Build both images from this source and start the two-service stack:

```bash
docker compose -f compose.lite.yaml up -d --build
```

The project name is `musicdl`, matching the full quick stack, and the same three application volume names and mount paths are used. The plugin runner remains non-root, read-only, resource-limited, and reachable only over the internal plugin-control network; it receives no app secrets. The app binds to loopback on port `8000` by default. A new install uses the initial `admin` / `password` credentials; change them immediately while keeping the service private. Existing deployments should follow the mode-switch procedure below rather than starting a second project against the same port or data volumes.

## Existing Repository Deployment

The repository deployment remains available for operators who already manage Redis separately or need the source-build Compose file. It requires a reachable Redis instance and a checkout of this repository:

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
cp .env.example .env
```

Set `MUSICDL_REDIS__URL` in `.env` to your Redis endpoint. Do not commit credentials. Then pull and start the published images:

```bash
MUSICDL_IMAGE_TAG=1.0.2 docker compose -f compose.prod.yaml pull
MUSICDL_IMAGE_TAG=1.0.2 docker compose -f compose.prod.yaml up -d
```

The source-build `compose.yaml` is also available when you want Docker to build from the checkout.

Check the service:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

By default, access the panel through an HTTPS reverse proxy. Before using [http://127.0.0.1:8000](http://127.0.0.1:8000) directly, apply the HTTP cookie settings under Configuration below. Sign in with the initial credentials supplied by your deployment. A fresh installation requires the default credentials to be replaced before the rest of the administration API can be used.

From the panel you can:

1. Import or manage compatible JavaScript source scripts.
2. Add and configure Telegram bot channels.
3. Search for a song and test a real download.
4. Configure WeCom, Telegram, AI assistance, timeouts, and runtime limits.
5. Inspect channel health, application events, audits, and service logs.

## Deployment

The currently published image pair is:

| Image | Purpose |
|---|---|
| `wit7zz/musicdl:1.0.2` | Main service and built-in administration panel |
| `wit7zz/musicdl-plugin-runner:1.0.2` | Isolated JavaScript plugin runtime |

Both `compose.quick.yaml` and `compose.prod.yaml` accept `MUSICDL_IMAGE_TAG`; use a published numbered version for predictable deployments. The quick Compose file now defaults to the published `1.0.2` images. The cookie-policy fix was introduced in `1.0.1` and is included in `1.0.2`; the original `v1.0.0` images do not include it. Docker image tags and GitHub source releases are separate release outputs; see [GitHub Releases](https://github.com/Gaoshou101/musicdl/releases) for source releases and their assets.

The production Compose file binds the application to `127.0.0.1` by default. Put it behind a TLS reverse proxy before exposing it outside the host. Example configurations are available for:

- [Caddy](./deploy/caddy/Caddyfile)
- [Nginx](./deploy/nginx/musicdl.conf)

Persistent data is stored in three Docker volumes:

| Volume | Content |
|---|---|
| `musicdl-media` | Downloaded media |
| `musicdl-app-data` | Administrator state and installed-source data |
| `musicdl-telegram` | Telegram session data |

Back up these volumes before upgrading or migrating a deployment.

## Migrating an Existing Deployment

The quick file declares the same three application volume keys as the repository Compose files, plus a new Redis volume. These are Compose logical keys; with default naming, the actual Docker volume names are `<project>_musicdl-media`, `<project>_musicdl-app-data`, and `<project>_musicdl-telegram`. The procedure below assumes those project-prefixed names and unchanged container mount paths. An explicit volume `name:`, `external: true`, bind mount, or override that changes a mount needs a separate mapping or copy plan; do not assume matching keys will reuse that data.

Before following these steps, open the admin panel's Runtime Configuration → Redis → Redis Address (`redis.url`) and check its source. Use this procedure only when the source is `Environment` (`env`) and you have confirmed the old deployment's `MUSICDL_REDIS__URL` is the intended external endpoint, or when the source is `Default` and you have independently verified the active endpoint is that intended external Redis. A panel override is persisted in `admin-state.json` on the reused app-data volume and takes precedence over Compose environment, so the quick file's internal Redis URL alone will not switch the app. If the source is `Panel override`, do not click `Restore deployment value` while the old app or workers are running: the secret is masked, and clearing it can hot-reload `redis.url` to a different endpoint immediately. Keep the external-Redis Compose deployment running and plan a controlled offline migration separately. If you cannot verify the source and intended endpoint, stay on the existing deployment.

Also review customized `wecom.api_base`, `telegram.proxy`, and `ai.base_url` values and any endpoint that names a service from an old Compose override. The quick manifest does not include old proxy/overlay services or automatically carry their environment wiring. Before cutover, verify each required endpoint remains reachable from the quick stack or map/replace it and provide its required environment settings. If a required proxy, service, or environment setting is missing, keep the old deployment until that dependency is mapped or replaced.

1. While the old stack is running, record its project name from `docker compose ls`, the exact ordered `-f` file list, any `--env-file` options, and its volume names and container mount paths (check the app container with `docker inspect`).
2. Stop the old stack before backing up, so its writers cannot change files during the backup. From the original project directory, run the same Compose file and environment arguments used to start it. For example:

   ```bash
   docker compose -p OLD_PROJECT -f compose.prod.yaml stop
   ```

   Replace `OLD_PROJECT` and the manifest arguments with the values recorded in step 1; repeat every `-f` in the same order and keep any `--env-file` options. Back up the actual volume names found in step 1, then back up external Redis from the same quiesced point using its own consistent procedure. If Redis is shared, quiesce its other writers too. See Docker's [volume backup and restore guidance](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes).
3. After the backups are complete, remove the old containers, networks, and orphans while retaining the volumes:

   ```bash
   docker compose -p OLD_PROJECT -f compose.prod.yaml down --remove-orphans
   ```

   Use the same original manifest and environment arguments as for `stop`. Do not add `-v`.
4. The quick stack creates a new, empty Redis. If existing Redis-backed state is disposable, start the stack:

   ```bash
   docker compose -p OLD_PROJECT -f compose.quick.yaml up -d
   ```

   If Redis-backed state must survive, start only the bundled Redis first, import the backed-up data, and verify it before starting the app:

   ```bash
   docker compose -p OLD_PROJECT -f compose.quick.yaml up -d redis
   # Restore/import the external Redis backup into the bundled Redis and verify it.
   docker compose -p OLD_PROJECT -f compose.quick.yaml up -d
   ```

   Replace `OLD_PROJECT` with the exact old name. If `compose.quick.yaml` is elsewhere, run from the original project directory and pass its absolute path, for example `-f /path/to/compose.quick.yaml`. The `-p` value overrides the quick file’s default project name. This reuses application volumes only when their actual names and container mount paths match; the new Redis volume is `<project>_musicdl-redis-data`. Until required Redis data is imported and verified, keep the new app stopped. The quick path suits fresh installs, migrations where old Redis state is disposable, or planned migrations where required Redis data and deployment-dependent settings are imported/mapped and verified before the app starts.
5. Verify the Redis source is `Environment` (`env`), `/readyz` reports general readiness, and sign-in, installed sources, media, and Telegram sessions work. Recheck custom `wecom.api_base`, `telegram.proxy`, and `ai.base_url` endpoints and run their relevant workflows. `/readyz` is not universal proof of Redis connectivity or integration reachability; test the Redis-backed and integration workflows you use. External Redis contents are not copied automatically, and the external instance is not modified.

To roll back the service deployment only, without rolling back persisted settings or data, remove the quick stack without deleting volumes, then start the original manifest set. For example:

```bash
docker compose -p OLD_PROJECT -f compose.quick.yaml down
docker compose -p OLD_PROJECT -f compose.prod.yaml up -d
```

Replace `OLD_PROJECT` and repeat the exact original manifest and environment arguments on the second command. If the quick file is elsewhere, pass its absolute path on the first command. Shared application volumes remain in place, including `admin-state.json` on the app-data volume, so panel settings and overrides are not reset by switching Compose files. Bundled Redis writes remain in the separate quick Redis volume instead of appearing in external Redis. For a full data/configuration rollback to the pre-migration state, stop writers and restore the application-volume snapshots (including the saved admin state) and matching external Redis backup taken at the quiesced point. Preserving later writes from bundled Redis instead requires a separate Redis export/import.

### Switching between full and lite mode

Lite has no Redis service and does not connect to Redis because it requires WeCom to be disabled. Before moving from full to lite, disable WeCom and save that change while still in full mode; otherwise lite startup rejects the enabled setting, including an enabled value saved in the app-data volume. Keep the full three-service quick install as the default when WeCom is required.

Before switching, inspect Runtime Configuration → Redis → Redis Address (`redis.url`) and its source, and record the active target. The Redis setting can remain saved in `admin-state.json` even though lite does not use Redis. If its source is `Panel override`, do not clear or restore it while the full app or workers are running: a saved secret is masked and clearing it may hot-reload full mode to another Redis endpoint. Decide which Redis target full mode should use before switching back.

Build the lite images from the current checkout before stopping the old stack, even though the published `1.0.2` image tags support lite. Record the old Compose project name, ordered manifests and env-file arguments, and actual volume names/mount paths. Keep the same project name (the default is `musicdl`) so the `musicdl-media`, `musicdl-app-data`, and `musicdl-telegram` application volume keys are reused. Stop the full stack and back up the app volumes before changing manifests. If you may need a full data rollback for a full quick install, also back up `musicdl-redis-data`. Compose `down` without `-v` retains that volume for a service rollback; the backup protects the Redis data for a full data rollback. If external Redis data is in scope for a full data rollback, take a consistent, provider-specific snapshot at the same quiesced point as the application-volume backups. For an existing external-Redis full deployment, keep the same intended Redis service available for when full mode is resumed; lite itself does not use that Redis endpoint.

```bash
docker compose -p OLD_PROJECT -f compose.lite.yaml build
docker compose -p OLD_PROJECT -f compose.quick.yaml stop
# Back up the actual application volumes and, when needed, Redis data at the same quiesced point.
docker compose -p OLD_PROJECT -f compose.quick.yaml down
docker compose -p OLD_PROJECT -f compose.lite.yaml up -d
```

Replace `OLD_PROJECT` and the old-manifest commands with the exact project and arguments recorded above; when migrating from a different full manifest, use it for both `stop` and `down`. Do not add `-v`. Run the lite command from the source checkout. Verify `/healthz`, `/readyz`, panel sign-in, media, Telegram sessions, and plugin operations you use. WeCom remains disabled in the shared saved app state.

For a service-only rollback, stop lite and start the original full manifest with the same project name and env-file arguments. This retains the application data and, for full quick installs, the untouched Redis volume. WeCom remains disabled until you explicitly re-enable it in full mode. Lite performs no Redis writes, but review the saved `redis.url` target before that full-mode restart. A data rollback is different: stop all writers and restore the matching pre-switch application-volume snapshots and, for full quick installs, the matching `musicdl-redis-data` backup. Never remove volumes as part of a service rollback.

## Configuration

The administration panel is the preferred place to manage runtime settings. Environment variables remain available for deployment-level configuration.

| Variable | Purpose |
|---|---|
| `MUSICDL_REDIS__URL` | Required external Redis connection for `compose.yaml` and `compose.prod.yaml`; full quick install uses bundled Redis; lite mode does not use Redis |
| `MUSICDL_PORT` | Host port bound to the main service; default `8000` |
| `MUSICDL_ADMIN__COOKIE_SECURE` | Whether administrator cookies require HTTPS; defaults to `true`, set `false` only for trusted host-local HTTP access via loopback |
| `MUSICDL_IMAGE_TAG` | Docker image version used by `compose.quick.yaml` and `compose.prod.yaml`; lite builds both images from source |
| `MUSICDL_DEPLOYMENT_MODE` | `lite` is set by `compose.lite.yaml`; other manifests use the default `full` mode |
| `MUSICDL_AI__ENABLED` | Enable optional OpenAI-compatible advisory features |
| `MUSICDL_AI__BASE_URL` | OpenAI-compatible API endpoint |
| `MUSICDL_AI__API_KEY` | API credential; keep it outside source control |
| `MUSICDL_AI__MODEL` | Model identifier supplied to the compatible endpoint |

Most changes made in the panel rebuild the active runtime without restarting the container. Settings that affect startup boundaries may still require a restart.

For direct HTTP from the deployment host via loopback, set `MUSICDL_ADMIN__COOKIE_SECURE=false`; keep the default `true` when an HTTPS reverse proxy is in front of the service. The Compose files bind to `127.0.0.1` by default; LAN access requires a separate intentional port-binding or proxy change. The quick manifest now defaults to `1.0.2`. The cookie-policy fix was introduced in the published `1.0.1` images and is included in `1.0.2`; the original `v1.0.0` images do not include it. For `compose.prod.yaml`, put the setting in `.env` and recreate the main service. When building a custom image, use source that includes the cookie-policy fix.

Custom Compose files must also add `MUSICDL_ADMIN__COOKIE_SECURE: "${MUSICDL_ADMIN__COOKIE_SECURE:-true}"` under `musicdl.environment`; `.env` alone does not pass it into the container. After upgrading and verifying HTTP login and password changes, remove any temporary `panel` proxy and configuration used only to strip Secure, and map the original entry port directly to the main service's port `8000`. HTTP sends credentials and sessions in plaintext; use HTTPS for public deployments.

## Source Scripts and Content Responsibility

musicdl does not bundle third-party music-source scripts in its release images. An administrator may import a compatible script through the panel after reviewing its analysis result and requested network access.

Imported scripts execute third-party logic and may contact external services. Review their source, license, network permissions, and legal status before installation.

This project does not grant rights to copyrighted media. You are responsible for complying with the terms of each provider and the laws that apply to your use.

## Security Model

The Compose configurations apply these boundaries:

- non-root UID/GID `10001:10001`;
- read-only container root filesystems;
- all Linux capabilities dropped;
- `no-new-privileges`;
- an internal-only plugin control network;
- no application secrets supplied to the plugin runner;
- bounded CPU, memory, temporary storage, and plugin execution;
- mandatory administrator credential replacement;
- session authentication, CSRF validation, audit records, and login rate limiting.

Imported code is still untrusted code. Keep the deployment private unless remote access is required, terminate TLS at a reverse proxy, and maintain tested volume backups.

## Development

The backend requires Python 3.12:

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest -q
```

Build the administration panel:

```bash
cd web
npm ci --no-audit --no-fund
npx tsc --noEmit
npm run build
```

Run the release-gate dry run:

```bash
python scripts/release/run_gates.py --self-test --dry-run
```

Some acceptance gates require real WeCom, Redis, Telegram, or Docker deployment infrastructure and therefore cannot be completed by a generic hosted runner.

## Release Channels

- [GitHub Releases](https://github.com/Gaoshou101/musicdl/releases)
- [Main Docker image](https://hub.docker.com/r/wit7zz/musicdl)
- [Plugin runner image](https://hub.docker.com/r/wit7zz/musicdl-plugin-runner)
- [Third-party dependency baseline](./THIRD_PARTY.md)

## Release Checklist

For each numbered release, synchronize package/version metadata, both README files, the GitHub Release, and matching version-tagged main and plugin-runner Docker images. Publish the release outputs from the same source commit and verify their provenance; confirm CI has completed successfully before announcing the release.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Gaoshou101%2Fmusicdl&type=Date)](https://star-history.com/#Gaoshou101/musicdl&Date)

The chart is provided by a third-party service and becomes available after the repository is public.

## License

musicdl is available under the [MIT License](./LICENSE).
