<h1 align="center">musicdl</h1>

<p align="center">Self-hosted music search and downloads, managed from one browser-based control panel.</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/Gaoshou101/musicdl/actions/workflows/ci.yml"><img src="https://github.com/Gaoshou101/musicdl/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://github.com/Gaoshou101/musicdl/releases"><img src="https://img.shields.io/github/v/release/Gaoshou101/musicdl?style=flat-square&color=F59E0B" alt="Latest release"></a>
  <a href="https://hub.docker.com/r/wit7zz/musicdl"><img src="https://img.shields.io/badge/Docker-amd64%20%7C%20arm64-4B5563?style=flat-square" alt="Docker architectures"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-22C55E?style=flat-square" alt="MIT License"></a>
</p>

musicdl searches music sources and downloads selected tracks to your own server. Its web panel also manages sources, download history, and optional messaging and AI integrations.

## Highlights

| Capability | What you can do |
|---|---|
| One control panel | Search, download, manage sources, and review service status in the browser. |
| Importable sources | Add JavaScript or Python sources and review requested network access before installing them. |
| Download history | Revisit results and saved files after a search or service restart. |
| Optional integrations | Configure WeCom, Telegram, and OpenAI-compatible AI features when you need them. |
| Isolated plugin runner | Run imported source code in a separate container with restricted privileges and resources. |

## Quick Install

Requirements: Docker Engine, Docker Compose v2.24.0 or newer, and internet access to pull the published images.

Create a project directory, download the single Compose file, and start musicdl. The file pins both application images to `1.1.5`:

```bash
mkdir -p musicdl
cd musicdl
curl -fsSLo compose.yaml https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.yaml
docker compose up -d
```

On Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path "musicdl" | Out-Null
Set-Location "musicdl"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.yaml" -OutFile "compose.yaml"
docker compose up -d
```

The file starts the web service, isolated plugin runner, and private Redis. No repository checkout, `.env` file, or separately managed Redis is needed for the default setup.

## Quick Start

1. Open [http://127.0.0.1:8000](http://127.0.0.1:8000). A fresh install starts with username `admin` and password `password`. Sign in and immediately set a new username and password; the new password must have at least 8 characters. Keep the service private until you change the default credentials.
2. In **音源管理** (Sources), choose **导入音源** (Import Sources). Paste the [source catalog URL](https://raw.githubusercontent.com/Gaoshou101/musicdl/main/sources/catalog.json), review each script and its requested network access, then install the sources you trust. The catalog lists third-party scripts; availability and license terms vary.
3. Use the dashboard search field to search by song or artist. Choose a result and select Download. Open **下载历史** (Download History) to review results and access saved files.

The default installation has no music scripts installed, so import at least one source before searching.

## Network Access and Configuration

The service binds to `127.0.0.1:8000` by default, so it is available from the Docker host only. `MUSICDL_ADMIN__COOKIE_SECURE` defaults to `false` in this Compose file so the login session works over local HTTP. The service itself does not provide TLS.

To change these defaults, create an optional `.env` file beside `compose.yaml`; it is not needed for the default setup.

| Variable | Default | Purpose |
|---|---|---|
| `MUSICDL_BIND_HOST` | `127.0.0.1` | Host interface to bind. Use `0.0.0.0` only when you intend to expose the panel on a trusted LAN. |
| `MUSICDL_PORT` | `8000` | Host port for the web panel. |
| `MUSICDL_IMAGE_TAG` | `1.1.5` | Version tag shared by the main and plugin-runner images. |
| `MUSICDL_ADMIN__COOKIE_SECURE` | `false` | Set to `true` when accessing the panel through HTTPS. |
| `MUSICDL_DEPLOYMENT_MODE` | `full` | Set to `lite` to disable WeCom in the app. The unified Compose stack still starts bundled Redis. |
| `MUSICDL_WECOM__ENABLED` | `false` | Enable or disable the WeCom app integration. Keep this `false` in lite mode. |

To make the panel available to other devices on a trusted LAN or NAS, create a `.env` file beside `compose.yaml` and set the bind address:

```dotenv
MUSICDL_BIND_HOST=0.0.0.0
MUSICDL_PORT=8000
```

Then apply the change with `docker compose up -d`. Binding to `0.0.0.0` exposes the port on all host interfaces. From another device on the LAN, open `http://<host-IP>:<port>` (port `8000` by default). Direct HTTP is unencrypted, so use an HTTPS reverse proxy for sensitive or remote access. Example configurations are available for [Caddy](./deploy/caddy/Caddyfile) and [Nginx](./deploy/nginx/musicdl.conf); adapt the sample hostnames and certificate paths to your setup. Set the cookie option in the same `.env` file:

```dotenv
MUSICDL_ADMIN__COOKIE_SECURE=true
```

Keep `MUSICDL_ADMIN__COOKIE_SECURE=false` only for direct HTTP access through the host's loopback address. HTTP sends credentials and session cookies without encryption; use HTTPS when accessing musicdl from other devices.

After signing in, use **运行配置** (Runtime Configuration) to review application settings. Settings marked as requiring a restart take effect after `docker compose restart musicdl`. Changes made in `.env` alter the container configuration; apply those with `docker compose up -d` so Compose recreates the service.

## Optional Integrations

No integration is needed for the browser search workflow. Runtime Configuration can enable optional OpenAI-compatible AI assistance and configure Telegram or WeCom credentials. Telegram also requires authorizing an account and configuring a bot. WeCom requires an allowed-user list and a reachable `/wecom/callback` endpoint; the bundled Redis service supports its state and job queue.

## Data, Updates, and Existing Installs

The Compose project is named `musicdl`. Persistent data is stored in named volumes; Docker prefixes the volume names with the Compose project name.

| Volume key | Container path | Contents |
|---|---|---|
| `musicdl-media` | `/data/music` | Downloaded music files. |
| `musicdl-app-data` | `/data/app` | Administrator state, runtime configuration, installed sources, and download history. |
| `musicdl-telegram` | `/data/telegram-sessions` | Telegram sessions. |
| `musicdl-redis-data` | `/data` | Bundled Redis persistence. |

Back up the volumes before a migration, and do not use `docker compose down -v` when you need to keep their contents.

The Compose file pins the image tag to `1.1.5`. To update, set `MUSICDL_IMAGE_TAG` in `.env` to the release you want, or download a Compose file whose default tag is that release. Then run these commands from the directory containing it:

```bash
docker compose pull
docker compose up -d
```

**Moving an existing installation:** back up first, then check these three items:

- **Application data:** inspect the old Compose project name, actual volume names, and mount paths. All three must match to reuse the data. To retain an old project name when the volume keys and mount paths also match, use `docker compose -p existing-project pull` and `docker compose -p existing-project up -d`, replacing `existing-project` with that name. Explicitly map or copy volumes whose names differ.
- **Redis data:** bundled Redis uses its own volume and does not copy external Redis data. WeCom users should plan and verify Redis state migration before switching, and keep the old Redis and its data intact until that work is complete.
- **Application configuration:** carry forward the old `.env` and other needed overrides, especially WeCom credentials and the allowed-user list.

For a source checkout, build both application images from the repository with a separate local tag:

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
export MUSICDL_IMAGE_TAG=local
docker compose build musicdl plugin-runner
docker compose up -d --no-build --pull missing
```

In Windows PowerShell, set the same tag before starting the build:

```powershell
$env:MUSICDL_IMAGE_TAG = "local"
docker compose build musicdl plugin-runner
docker compose up -d --no-build --pull missing
```

## Startup Checks and Common Issues

From the directory containing `compose.yaml`, check all three services and their recent logs:

```bash
docker compose ps
docker compose logs --tail=100 musicdl plugin-runner redis
```

- **The page does not open:** check that all three services are `healthy`. For a NAS or server install, set the bind address as described under Network Access and Configuration and open the page using that device's IP.
- **The port is already in use:** set `MUSICDL_PORT=8001` in `.env`, run `docker compose up -d`, and use the new port in your browser.
- **Signing in returns to the login page:** use `MUSICDL_ADMIN__COOKIE_SECURE=false` for local HTTP and `true` for HTTPS. Apply changes with `docker compose up -d`.
- **Search returns no results:** import and enable a source first, then check its status in Sources.

## Documentation

| Topic | Guide |
|---|---|
| Available source scripts and import instructions | [Source catalog](./sources/README.md) |
| Published versions and release notes | [GitHub Releases](https://github.com/Gaoshou101/musicdl/releases) |
| Container images | [Docker Hub](https://hub.docker.com/r/wit7zz/musicdl) |

## License

[MIT](./LICENSE)
