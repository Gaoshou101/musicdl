<h1 align="center">musicdl</h1>

<p align="center">A self-hosted, multi-source music download service with a Chinese admin panel, WeCom workflows, Telegram sources, and isolated JavaScript plugins.</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
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

## Quick Install

Requirements:

- Docker Engine with Docker Compose
- A reachable Redis instance
- Git, if you want to use the repository Compose file

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
cp .env.example .env
```

Set `MUSICDL_REDIS__URL` in `.env` to your Redis endpoint. Do not commit credentials.

## Quick Start

Pull the published v1.0.0 images and start the service:

```bash
export MUSICDL_IMAGE_TAG=1.0.0
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d
```

Check the service:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with the initial credentials supplied by your deployment. A fresh installation requires the default credentials to be replaced before the rest of the administration API can be used. The default password is intentionally not repeated in this public README.

From the panel you can:

1. Import or manage compatible JavaScript source scripts.
2. Add and configure Telegram bot channels.
3. Search for a song and test a real download.
4. Configure WeCom, Telegram, AI assistance, timeouts, and runtime limits.
5. Inspect channel health, application events, audits, and service logs.

## Deployment

The release consists of two images:

| Image | Purpose |
|---|---|
| `wit7zz/musicdl:1.0.0` | Main service and built-in administration panel |
| `wit7zz/musicdl-plugin-runner:1.0.0` | Isolated JavaScript plugin runtime |

Use `MUSICDL_IMAGE_TAG=latest` to follow the newest published image, or pin a numbered version for predictable deployments.

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

## Configuration

The administration panel is the preferred place to manage runtime settings. Environment variables remain available for deployment-level configuration.

| Variable | Purpose |
|---|---|
| `MUSICDL_REDIS__URL` | Required Redis connection URL |
| `MUSICDL_PORT` | Host port bound to the main service; default `8000` |
| `MUSICDL_ADMIN__COOKIE_SECURE` | Whether administrator cookies require HTTPS; defaults to `true`, set `false` only for a trusted direct HTTP deployment |
| `MUSICDL_IMAGE_TAG` | Docker image version used by `compose.prod.yaml` |
| `MUSICDL_AI__ENABLED` | Enable optional OpenAI-compatible advisory features |
| `MUSICDL_AI__BASE_URL` | OpenAI-compatible API endpoint |
| `MUSICDL_AI__API_KEY` | API credential; keep it outside source control |
| `MUSICDL_AI__MODEL` | Model identifier supplied to the compatible endpoint |

Most changes made in the panel rebuild the active runtime without restarting the container. Settings that affect startup boundaries may still require a restart.

For a direct HTTP panel, set `MUSICDL_ADMIN__COOKIE_SECURE=false` in `.env` and recreate the main service. Keep the default `true` when an HTTPS reverse proxy is in front of the service.

## Source Scripts and Content Responsibility

musicdl does not bundle third-party music-source scripts in its release images. An administrator may import a compatible script through the panel after reviewing its analysis result and requested network access.

Imported scripts execute third-party logic and may contact external services. Review their source, license, network permissions, and legal status before installation.

This project does not grant rights to copyrighted media. You are responsible for complying with the terms of each provider and the laws that apply to your use.

## Security Model

The production Compose configuration applies these boundaries:

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

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Gaoshou101%2Fmusicdl&type=Date)](https://star-history.com/#Gaoshou101/musicdl&Date)

The chart is provided by a third-party service and becomes available after the repository is public.

## License

musicdl is available under the [MIT License](./LICENSE).
