<h1 align="center">musicdl</h1>

<p align="center">自托管的音乐搜索与下载服务，通过浏览器管理音源、任务和运行状态。</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="./web/public/brand-options/social-preview-cool-aurora-glass.png" alt="musicdl 冷瓷极光玻璃视觉横幅" width="720">
</p>

<p align="center">
  <a href="https://github.com/Gaoshou101/musicdl/actions/workflows/ci.yml"><img src="https://github.com/Gaoshou101/musicdl/actions/workflows/ci.yml/badge.svg" alt="CI 状态"></a>
  <a href="https://github.com/Gaoshou101/musicdl/releases"><img src="https://img.shields.io/github/v/release/Gaoshou101/musicdl?style=flat-square&color=F59E0B" alt="最新版本"></a>
  <a href="https://hub.docker.com/r/wit7zz/musicdl"><img src="https://img.shields.io/badge/Docker-amd64%20%7C%20arm64-4B5563?style=flat-square" alt="Docker 架构"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-22C55E?style=flat-square" alt="MIT 许可证"></a>
</p>

musicdl 可以搜索多个音乐音源，并将选中的歌曲下载到自己的服务器。管理面板还可以管理音源、下载历史，以及可选的消息平台和 AI 集成。

## 功能概览

| 能力 | 可完成的操作 |
|---|---|
| 统一管理面板 | 在浏览器中搜索、下载、管理音源并查看服务状态。 |
| 导入音源 | 添加 JavaScript 或 Python 音源，并在安装前检查脚本请求的网络访问权限。 |
| 下载历史 | 搜索或服务重启后，仍可查看已保存的结果和文件。 |
| 可选集成 | 按需配置企业微信、Telegram 和 OpenAI 兼容的 AI 功能。 |
| 隔离的插件运行器 | 在独立容器中运行导入的音源脚本，并限制其权限和资源。 |

## 快速安装

需要安装 Docker Engine、Docker Compose v2.24.0 或更新版本，并能联网拉取已发布的镜像。

先创建项目目录，再下载唯一的 Compose 文件并启动 musicdl。该文件将两个应用镜像固定为 `1.2.0`：

```bash
mkdir -p musicdl
cd musicdl
curl -fsSLo compose.yaml https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.yaml
docker compose up -d
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force -Path "musicdl" | Out-Null
Set-Location "musicdl"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.yaml" -OutFile "compose.yaml"
docker compose up -d
```

该文件会启动 Web 服务、隔离的插件运行器和私有 Redis。默认安装无需克隆仓库、创建 `.env` 文件或单独准备 Redis。

## 快速开始

1. 打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。全新安装的用户名是 `admin`，密码是 `password`。登录后立即更换用户名和密码；新密码至少需要 8 个字符。更换默认凭据前，请保持服务仅供本机访问。
2. 打开「音源管理」，选择「导入音源」，粘贴[音源目录 URL](https://raw.githubusercontent.com/Gaoshou101/musicdl/main/sources/catalog.json)。安装前请检查每个脚本及其请求的网络权限，并只安装你信任的音源。目录中的脚本由第三方提供，可用性和许可条款各不相同。
3. 在仪表板搜索歌曲名或艺人名，选择搜索结果并点击「下载」。打开「下载历史」查看结果和已保存的文件。

初始安装不含音乐脚本；请先导入至少一个音源，再开始搜索。

## 网络访问与配置

服务默认只监听 `127.0.0.1:8000`，因此只有 Docker 主机可以访问。Compose 文件将 `MUSICDL_ADMIN__COOKIE_SECURE` 默认设为 `false`，这样登录会话可以通过本机 HTTP 工作。服务本身不提供 TLS。

如需修改这些默认值，请在 `compose.yaml` 同目录创建可选的 `.env` 文件；默认部署无需创建。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `MUSICDL_BIND_HOST` | `127.0.0.1` | 宿主机监听地址。只有确需在可信局域网开放管理面板时才设为 `0.0.0.0`。 |
| `MUSICDL_PORT` | `8000` | 管理面板映射到宿主机的端口。 |
| `MUSICDL_IMAGE_TAG` | `1.2.0` | 主服务和插件运行器共用的镜像版本标签。 |
| `MUSICDL_ADMIN__COOKIE_SECURE` | `false` | 通过 HTTPS 访问面板时设为 `true`。 |
| `MUSICDL_DEPLOYMENT_MODE` | `full` | 设为 `lite` 可在应用中禁用企业微信；统一 Compose 栈仍会启动内置 Redis。 |
| `MUSICDL_WECOM__ENABLED` | `false` | 启用或禁用企业微信集成。lite 模式下请保持为 `false`。 |

如果确实需要让可信局域网中的其他设备或 NAS 用户访问管理面板，请在 `compose.yaml` 同目录创建 `.env`，设置监听地址：

```dotenv
MUSICDL_BIND_HOST=0.0.0.0
MUSICDL_PORT=8000
```

然后运行 `docker compose up -d` 应用设置。绑定到 `0.0.0.0` 会在主机的所有网络接口上开放端口。局域网中的其他设备可打开 `http://<主机IP>:<端口>`（默认端口 `8000`）。直接使用 HTTP 时数据不会加密；访问敏感数据或从远程访问时，请配置 HTTPS 反向代理。仓库提供了 [Caddy](./deploy/caddy/Caddyfile) 和 [Nginx](./deploy/nginx/musicdl.conf) 示例；请按实际情况修改示例域名和证书路径，并在同一个 `.env` 文件中设置 Cookie 选项：

```dotenv
MUSICDL_ADMIN__COOKIE_SECURE=true
```

只有通过主机环回地址直接使用 HTTP 时，才应保留 `MUSICDL_ADMIN__COOKIE_SECURE=false`。HTTP 不会加密登录凭据和会话 Cookie；从其他设备访问时请使用 HTTPS。

登录后可在「运行配置」中查看应用设置。标记为需要重启的设置，执行 `docker compose restart musicdl` 后生效。修改 `.env` 会改变容器配置，应执行 `docker compose up -d`，让 Compose 重新创建服务。

## 可选集成

浏览器搜索无需配置其他集成。「运行配置」中可以启用 OpenAI 兼容 AI 辅助功能，或填写 Telegram 和企业微信凭据。Telegram 还需要授权账号并配置 Bot；企业微信需要配置允许访问的用户列表，并让 `/wecom/callback` 地址可从外部访问。内置 Redis 为企业微信状态和任务队列提供支持。

## 数据、更新和已有部署

Compose 项目名为 `musicdl`，数据保存在具名卷中；Docker 会在卷的实际名称前加上 Compose 项目名。

| 卷键 | 容器路径 | 内容 |
|---|---|---|
| `musicdl-media` | `/data/music` | 已下载的音乐文件。 |
| `musicdl-app-data` | `/data/app` | 管理员状态、运行配置、已安装音源和下载历史。 |
| `musicdl-telegram` | `/data/telegram-sessions` | Telegram 会话。 |
| `musicdl-redis-data` | `/data` | 内置 Redis 持久化数据。 |

迁移前请备份数据卷；要保留数据时，不要运行 `docker compose down -v`。

Compose 文件默认固定使用 `1.2.0` 镜像。更新时，先在 `.env` 中将 `MUSICDL_IMAGE_TAG` 改为目标版本，或下载默认标签已更新到目标版本的 Compose 文件。`1.1.5` 镜像对仍可用于回滚。然后在该文件所在目录执行：

```bash
docker compose pull
docker compose up -d
```

**已有部署迁移**：切换前先备份，并检查以下三项：

- **应用数据**：核对旧 Compose 项目名、实际卷名和挂载路径，三者匹配才会复用数据。若需要保留旧项目名，且卷键和挂载路径也匹配，升级命令使用 `docker compose -p existing-project pull` 和 `docker compose -p existing-project up -d`；将 `existing-project` 换成实际项目名。名称不同的卷需要明确映射或复制。
- **Redis 数据**：内置 Redis 使用自己的卷，不会自动复制外部 Redis 数据。企业微信用户应先规划并验证 Redis 状态迁移，确认完成前保留原 Redis 及其数据。
- **应用配置**：保留原 `.env` 和其他需要的覆盖设置，尤其是企业微信凭据和允许访问的用户列表。

从源码检出目录构建两个应用镜像时，请使用单独的本地标签：

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
export MUSICDL_IMAGE_TAG=local
docker compose build musicdl plugin-runner
docker compose up -d --no-build --pull missing
```

Windows PowerShell：

```powershell
$env:MUSICDL_IMAGE_TAG = "local"
docker compose build musicdl plugin-runner
docker compose up -d --no-build --pull missing
```

## 启动检查与常见问题

在 `compose.yaml` 所在目录执行以下命令，查看三个服务的状态和最近的日志：

```bash
docker compose ps
docker compose logs --tail=100 musicdl plugin-runner redis
```

- **页面打不开**：先确认三个服务均为 `healthy`。在 NAS 或服务器上安装时，请按「网络访问与配置」设置监听地址，并用该设备的 IP 打开页面。
- **端口被占用**：在 `.env` 中设置 `MUSICDL_PORT=8001`，执行 `docker compose up -d` 后改用新端口访问。
- **登录后又回到登录页**：本机 HTTP 使用 `MUSICDL_ADMIN__COOKIE_SECURE=false`；HTTPS 使用 `true`。修改后执行 `docker compose up -d`。
- **搜索没有结果**：先确认已导入并启用音源，再在「音源管理」中检查音源状态。

## 更多信息

| 主题 | 资料 |
|---|---|
| 可选音源脚本与导入步骤 | [音源目录说明](./sources/README.md) |
| 已发布版本与更新说明 | [GitHub Releases](https://github.com/Gaoshou101/musicdl/releases) |
| 容器镜像 | [Docker Hub](https://hub.docker.com/r/wit7zz/musicdl) |

## Star 历史

[![Star History Chart](https://api.star-history.com/svg?repos=Gaoshou101%2Fmusicdl&type=Date)](https://star-history.com/#Gaoshou101/musicdl&Date)

该图表由第三方服务动态生成，仓库公开后才可正常读取数据。

## 许可证

[MIT](./LICENSE)
