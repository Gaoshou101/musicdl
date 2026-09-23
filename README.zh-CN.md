<h1 align="center">musicdl</h1>

<p align="center">自托管的多音源音乐下载服务，提供中文管理面板、企业微信交互、Telegram 音源和隔离的 JavaScript 插件运行环境。</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/Gaoshou101/musicdl/actions/workflows/ci.yml"><img src="https://github.com/Gaoshou101/musicdl/actions/workflows/ci.yml/badge.svg" alt="CI 状态"></a>
  <a href="https://github.com/Gaoshou101/musicdl/releases"><img src="https://img.shields.io/github/v/release/Gaoshou101/musicdl?style=flat-square&color=F59E0B" alt="最新版本"></a>
  <a href="https://hub.docker.com/r/wit7zz/musicdl"><img src="https://img.shields.io/badge/Docker-amd64%20%7C%20arm64-4B5563?style=flat-square" alt="Docker 架构"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-22C55E?style=flat-square" alt="MIT 许可证"></a>
</p>

## 核心亮点

| 亮点 | 对使用者的价值 |
|---|---|
| 多音源搜索与下载 | 在一个界面中搜索已安装音源，并在某个渠道失败时尝试其他可用渠道。 |
| 内置中文管理面板 | 管理音源、Telegram Bot、运行配置、渠道健康度、下载、事件、审计和服务日志。 |
| 可导入 JavaScript 音源 | 导入前分析兼容的 LX Music 音源脚本，第三方脚本不会被直接打包进镜像。 |
| 企业微信与 Telegram | 可在企业微信中直接发送歌名，也可将 Telegram 音乐 Bot 注册为额外渠道。 |
| 隔离插件运行环境 | JavaScript 插件在独立的非 root 容器中运行，根文件系统只读、无 Linux capabilities，并受到资源限制。 |
| 可复现部署 | 为 `linux/amd64` 和 `linux/arm64` 发布带版本号的 Docker 镜像。 |

## 架构

```text
┌────────────────┐  HTTPS  ┌──────────────────────────────┐
│ 浏览器 / 管理台│────────▶│        musicdl 主服务        │
└────────────────┘         │  API、面板、搜索、任务处理   │
                           └──────────────────────────────┘
┌────────────────┐                     │
│    企业微信    │◀──── 回调 ──────────┤
└────────────────┘                     │
                                      ├──────────────▶┌──────────────┐
┌────────────────┐                     │  状态与任务    │    Redis     │
│    Telegram    │◀──── Telethon ──────┤               └──────────────┘
└────────────────┘                     │
                                      │ 内部 HTTP
                                      ▼
                           ┌──────────────────────────────┐
                           │       隔离插件运行器         │
                           │  Deno、seccomp、不接收密钥   │
                           └──────────────────────────────┘
                                      │
                                      ▼
                           ┌──────────────────────────────┐
                           │ 导入音源与音乐服务端点       │
                           └──────────────────────────────┘
```

管理面板已经构建进主镜像。只有插件运行器保留为独立容器，因为它负责执行导入的代码。

## 快速安装

运行条件：

- Docker Engine 和 Docker Compose
- 一个可访问的 Redis 实例
- 如需使用仓库中的 Compose 文件，需要安装 Git

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
cp .env.example .env
```

将 `.env` 中的 `MUSICDL_REDIS__URL` 修改为你的 Redis 地址。不要提交真实凭据。

## 快速开始

拉取 v1.0.0 镜像并启动服务：

```bash
export MUSICDL_IMAGE_TAG=1.0.0
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d
```

检查服务状态：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，使用部署提供的初始账密登录。新部署必须先修改默认账密，之后才能使用其余管理接口。出于安全原因，公开 README 不重复默认密码。

进入面板后可以：

1. 导入或管理兼容的 JavaScript 音源脚本。
2. 添加并配置 Telegram Bot 渠道。
3. 搜索歌曲并执行真实下载测试。
4. 配置企业微信、Telegram、AI 辅助、超时和运行限制。
5. 查看渠道健康度、应用事件、审计记录和完整服务日志。

## 部署

正式版本包含两个镜像：

| 镜像 | 用途 |
|---|---|
| `wit7zz/musicdl:1.0.0` | 主服务和内置管理面板 |
| `wit7zz/musicdl-plugin-runner:1.0.0` | 隔离的 JavaScript 插件运行环境 |

设置 `MUSICDL_IMAGE_TAG=latest` 可以跟随最新镜像；正式环境建议固定具体版本，便于稳定升级和回滚。

生产 Compose 默认只监听 `127.0.0.1`。如需从其他设备访问，请先通过支持 TLS 的反向代理对外提供服务。仓库提供了以下示例：

- [Caddy](./deploy/caddy/Caddyfile)
- [Nginx](./deploy/nginx/musicdl.conf)

持久数据保存在三个 Docker 卷中：

| 数据卷 | 内容 |
|---|---|
| `musicdl-media` | 已下载媒体 |
| `musicdl-app-data` | 管理员状态和已安装音源数据 |
| `musicdl-telegram` | Telegram 会话数据 |

升级或迁移前应备份这三个数据卷。

## 配置

建议通过管理面板维护运行配置。环境变量主要用于部署层参数。

| 变量 | 用途 |
|---|---|
| `MUSICDL_REDIS__URL` | 必填的 Redis 连接地址 |
| `MUSICDL_PORT` | 主服务绑定到宿主机的端口，默认为 `8000` |
| `MUSICDL_ADMIN__COOKIE_SECURE` | 管理员 Cookie 是否只通过 HTTPS 发送，默认为 `true`；直接使用可信 HTTP 局域网部署时设为 `false` |
| `MUSICDL_IMAGE_TAG` | `compose.prod.yaml` 使用的 Docker 镜像版本 |
| `MUSICDL_AI__ENABLED` | 开启可选的 OpenAI 兼容 AI 辅助功能 |
| `MUSICDL_AI__BASE_URL` | OpenAI 兼容 API 地址 |
| `MUSICDL_AI__API_KEY` | API 凭据，不要写入版本库 |
| `MUSICDL_AI__MODEL` | 兼容端点使用的模型标识 |

面板保存的大部分配置会触发运行时热重载，不需要重启容器。涉及启动边界的设置仍可能要求重启。

如果主服务直接通过 HTTP 提供面板，请将 `MUSICDL_ADMIN__COOKIE_SECURE=false` 写入 `.env` 并重新创建主服务；如果前面有 HTTPS 反向代理，请保持默认值 `true`。

## 音源脚本与内容责任

musicdl 的发布镜像不内置第三方音乐音源脚本。管理员可以在审查脚本分析结果和出网权限后，通过管理面板导入兼容脚本。

导入的脚本会执行第三方逻辑，并可能访问外部服务。安装前请自行检查源码、许可证、出网权限和法律风险。

本项目不授予任何受版权保护媒体的使用权。你需要自行遵守各服务提供方的条款和所在地法律。

## 安全边界

生产 Compose 配置包含以下限制：

- 使用非 root 身份 `10001:10001`；
- 容器根文件系统只读；
- 移除全部 Linux capabilities；
- 启用 `no-new-privileges`；
- 插件控制网络仅限内部访问；
- 插件运行器不接收应用密钥；
- 限制 CPU、内存、临时存储和插件执行时间；
- 强制修改管理员默认账密；
- 提供会话认证、CSRF 校验、审计记录和登录限速。

导入脚本仍然属于不受信任代码。除非确有远程访问需求，否则建议保持服务私有；对外开放时应启用 TLS，并定期验证数据卷备份。

## 开发

后端要求 Python 3.12：

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest -q
```

构建管理面板：

```bash
cd web
npm ci --no-audit --no-fund
npx tsc --noEmit
npm run build
```

检查发布门禁：

```bash
python scripts/release/run_gates.py --self-test --dry-run
```

部分验收项依赖真实的企业微信、Redis、Telegram 或 Docker 部署环境，因此无法由通用 GitHub 托管 Runner 完成。

## 发布渠道

- [GitHub Releases](https://github.com/Gaoshou101/musicdl/releases)
- [主服务 Docker 镜像](https://hub.docker.com/r/wit7zz/musicdl)
- [插件运行器镜像](https://hub.docker.com/r/wit7zz/musicdl-plugin-runner)
- [第三方依赖基线](./THIRD_PARTY.md)

## Star 历史

[![Star History Chart](https://api.star-history.com/svg?repos=Gaoshou101%2Fmusicdl&type=Date)](https://star-history.com/#Gaoshou101/musicdl&Date)

该图表由第三方服务动态生成，仓库公开后才可正常读取数据。

## 许可证

musicdl 基于 [MIT 许可证](./LICENSE) 发布。

