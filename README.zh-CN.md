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

- Docker Engine 和 Docker Compose 插件
- 可访问互联网以拉取已发布的镜像

```bash
curl -fsSLo compose.quick.yaml https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.quick.yaml
docker compose -f compose.quick.yaml up -d
```

Windows PowerShell 可用以下命令下载同一个 Compose 文件并启动：

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.quick.yaml -OutFile compose.quick.yaml
docker compose -f .\compose.quick.yaml up -d
```

快速安装使用预构建镜像，并包含一个私有 Redis 服务。无需克隆仓库、创建 `.env` 或单独准备 Redis。Redis 不映射宿主机端口，数据保存在 Docker 卷中，只有主服务可以访问。插件运行器仍位于独立的内部控制网络中，不接收应用密钥。Web 服务默认只绑定到 `127.0.0.1:8000`。

默认情况下，管理员会话和 CSRF Cookie 需要 HTTPS（`MUSICDL_ADMIN__COOKIE_SECURE=true`），建议通过 HTTPS 反向代理访问。若要持续从 Docker 主机通过 `127.0.0.1` 直接使用 HTTP，请在 `compose.quick.yaml` 同目录创建或编辑 `.env` 文件，并添加：

```dotenv
MUSICDL_ADMIN__COOKIE_SECURE=false
```

然后重新创建主服务：

```bash
docker compose -f compose.quick.yaml up -d --force-recreate musicdl
```

快速安装默认无需 `.env` 文件；未设置该变量时，Cookie 仍仅通过 HTTPS 发送。将该行保留在 `.env` 中可使后续 Compose 重建继续使用 HTTP 设置。要恢复安全默认值，请删除这一行并强制重新创建 `musicdl`。快速安装默认仅绑定到环回地址，不会向局域网其他设备开放；如需局域网访问，必须另行有意修改端口绑定或反向代理配置。

全新快速安装的初始用户名为 `admin`，密码为 `password`。请立即设置新的强密码；面板会在凭据更改完成前阻止其余管理 API 的使用。默认凭据仍有效期间，请保持服务仅通过环回地址访问且不对外开放。

请在使用预期的 Cookie 设置启动服务后检查状态：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

然后打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。HTTP 会以明文传输账密和会话 Cookie，因此直接 HTTP 仅限可信的主机本地访问，公网访问请使用 HTTPS。

快速安装的 Compose 项目名默认为 `musicdl`。如需更改宿主机端口，可在运行 Compose 前设置 `MUSICDL_PORT`。如需选择其他已发布的应用镜像，可设置 `MUSICDL_IMAGE_TAG`；默认使用已发布的 `1.0.1` 镜像标签。主服务与插件运行器镜像使用相同的版本标签。

## 现有仓库部署方式

已自行管理 Redis，或需要从源码构建的用户仍可使用仓库内的 Compose 文件。该方式要求可访问的 Redis 实例以及本仓库代码副本：

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
cp .env.example .env
```

将 `.env` 中的 `MUSICDL_REDIS__URL` 设置为你的 Redis 地址。不要提交真实凭据。然后拉取并启动已发布镜像：

```bash
MUSICDL_IMAGE_TAG=1.0.1 docker compose -f compose.prod.yaml pull
MUSICDL_IMAGE_TAG=1.0.1 docker compose -f compose.prod.yaml up -d
```

需要从当前代码构建时，也可以使用 `compose.yaml`。

检查服务状态：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

默认通过 HTTPS 反向代理访问面板。直接访问 [http://127.0.0.1:8000](http://127.0.0.1:8000) 前，请完成下方「配置」中的 HTTP Cookie 设置。使用部署提供的初始账密登录；新部署必须先修改默认账密，之后才能使用其余管理接口。

进入面板后可以：

1. 导入或管理兼容的 JavaScript 音源脚本。
2. 添加并配置 Telegram Bot 渠道。
3. 搜索歌曲并执行真实下载测试。
4. 配置企业微信、Telegram、AI 辅助、超时和运行限制。
5. 查看渠道健康度、应用事件、审计记录和完整服务日志。

## 部署

当前已发布的镜像对为：

| 镜像 | 用途 |
|---|---|
| `wit7zz/musicdl:1.0.1` | 主服务和内置管理面板 |
| `wit7zz/musicdl-plugin-runner:1.0.1` | 隔离的 JavaScript 插件运行环境 |

`compose.quick.yaml` 和 `compose.prod.yaml` 都支持通过 `MUSICDL_IMAGE_TAG` 选择镜像版本。为便于稳定升级和回滚，建议固定已发布的具体版本。快速安装默认使用已包含 Cookie 策略修复的 `1.0.1` 镜像；原始 `v1.0.0` 镜像不包含该修复。`1.0.1` 是 Docker 镜像标签，不是 GitHub/源码发布标签。

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

## 从现有部署迁移

快速安装文件声明了与仓库 Compose 文件相同的三个应用卷键，并新增一个 Redis 卷。这些是 Compose 逻辑键；默认命名时，Docker 中的实际卷名为 `<项目名>_musicdl-media`、`<项目名>_musicdl-app-data` 和 `<项目名>_musicdl-telegram`。以下步骤假设使用这些带项目名前缀的卷名，且容器挂载路径未变更。如果旧配置为卷指定了 `name:`、使用 `external: true`、绑定挂载，或通过覆盖文件改了挂载路径，需要另行规划映射或复制；相同的逻辑键不代表会自动复用数据。

按以下步骤迁移前，先打开管理面板的「运行配置」→「Redis」→「Redis 地址」（`redis.url`），检查配置来源。只有来源为「部署变量」（`env`），且已确认旧部署的 `MUSICDL_REDIS__URL` 是目标外部地址时，才适用本流程；如果来源为「默认值」，也必须独立核实当前生效地址确实是目标外部 Redis。面板覆盖会保存在复用的应用数据卷中的 `admin-state.json`，并优先于 Compose 环境变量；因此仅在快速安装文件中设置内置 Redis 地址，并不能保证应用切换过去。如果来源显示「面板覆盖」，旧应用或 Worker 运行期间不要点击「恢复为部署值」：Redis 密钥会被隐藏，清除覆盖可能立即热重载 `redis.url` 并切换到其他地址。请保持旧的外部 Redis Compose 部署运行，另行规划受控的离线迁移。如果无法确认来源和目标地址，请继续使用现有部署。

同时检查自定义的 `wecom.api_base`、`telegram.proxy`、`ai.base_url`，以及旧 Compose 覆盖文件中引用的服务地址。快速安装清单不包含旧代理/覆盖服务，也不会自动带上它们的环境配置。切换前，请确认每个必需地址在快速安装网络中仍可访问，或先映射/替换地址并补齐所需环境设置。如果缺少必需的代理、服务或环境变量，请继续使用旧部署，直到依赖已映射或替换。

1. 旧服务仍运行时，从 `docker compose ls` 记录原项目名、完整且有顺序的 `-f` 文件列表、所有 `--env-file` 参数，以及卷名和容器内挂载路径（用 `docker inspect` 检查主服务容器）。
2. 先停止旧栈，再备份，以免服务在备份过程中继续写入文件。在原项目目录执行与启动旧栈时相同的 Compose 文件和环境参数。例如：

   ```bash
   docker compose -p OLD_PROJECT -f compose.prod.yaml stop
   ```

   将 `OLD_PROJECT` 和清单参数替换为步骤 1 记录的值；按原顺序重复所有 `-f`，并保留 `--env-file` 参数。备份步骤 1 查到的实际卷名，再在同一静止时间点使用外部 Redis 自身的一致性备份方式备份数据。如果 Redis 由其他服务共享，也要暂停那些写入方。可参考 Docker 的[数据卷备份与恢复说明](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)。
3. 备份完成后，移除旧容器、网络和孤立服务，但保留数据卷：

   ```bash
   docker compose -p OLD_PROJECT -f compose.prod.yaml down --remove-orphans
   ```

   使用与 `stop` 相同的原始清单和环境参数。不要添加 `-v`。
4. 快速安装会创建一个新的空 Redis。如果旧 Redis 状态可以丢弃，直接启动新栈：

   ```bash
   docker compose -p OLD_PROJECT -f compose.quick.yaml up -d
   ```

   如果必须保留 Redis 中的状态，先只启动内置 Redis，导入备份并验证后，再启动应用：

   ```bash
   docker compose -p OLD_PROJECT -f compose.quick.yaml up -d redis
   # 将外部 Redis 备份恢复/导入到内置 Redis，并验证数据。
   docker compose -p OLD_PROJECT -f compose.quick.yaml up -d
   ```

   将 `OLD_PROJECT` 替换为原项目名。如果 `compose.quick.yaml` 不在原目录，请在原项目目录执行，并传入该文件的绝对路径，例如 `-f /path/to/compose.quick.yaml`。`-p` 会覆盖快速安装文件中的默认项目名。只有实际卷名和容器挂载路径一致时，应用卷才会复用；新的 Redis 卷名为 `<项目名>_musicdl-redis-data`。必需的 Redis 数据导入并验证前，请保持新应用为停止状态。快速安装适用于全新部署、旧 Redis 状态可以丢弃的迁移，或已在启动应用前完成必需 Redis 数据导入、部署依赖配置映射并验证的迁移。
5. 确认 Redis 配置来源显示「部署变量」（`env`）、`/readyz` 报告服务总体就绪，并检查登录、已安装音源、媒体和 Telegram 会话。复查自定义的 `wecom.api_base`、`telegram.proxy`、`ai.base_url` 地址并验证对应工作流。`/readyz` 不能普遍证明所有工作流都能连接 Redis 或外部集成；还应实际验证你使用的 Redis 相关及集成工作流。外部 Redis 内容不会自动复制，外部实例也不会被修改。

只回滚服务、不回滚已保存设置或数据时，移除快速安装栈但保留卷，然后重新启动原清单。例如：

```bash
docker compose -p OLD_PROJECT -f compose.quick.yaml down
docker compose -p OLD_PROJECT -f compose.prod.yaml up -d
```

将 `OLD_PROJECT` 替换为原项目名；第二条命令需重复原来的完整清单和环境参数。如果快速安装文件在其他位置，第一条命令传入其绝对路径。共享应用卷仍会保留，其中 app-data 卷内的 `admin-state.json` 也会保留，因此切换 Compose 文件不会重置面板配置和覆盖。内置 Redis 的写入仍在快速安装的独立 Redis 卷中，不会出现在旧外部 Redis 中。若要将数据和配置完整回滚到迁移前状态，需先停止写入方，再恢复迁移前备份的应用卷（包括已保存的后台状态）和同一静止时间点的外部 Redis 备份。若要保留快速安装期间写入内置 Redis 的新数据，则需另行导出并导入 Redis 数据。

## 配置

建议通过管理面板维护运行配置。环境变量主要用于部署层参数。

| 变量 | 用途 |
|---|---|
| `MUSICDL_REDIS__URL` | `compose.yaml` 和 `compose.prod.yaml` 所需的外部 Redis 连接地址；快速安装使用内置 Redis |
| `MUSICDL_PORT` | 主服务绑定到宿主机的端口，默认为 `8000` |
| `MUSICDL_ADMIN__COOKIE_SECURE` | 管理员 Cookie 是否只通过 HTTPS 发送，默认为 `true`；仅可信的主机本地 HTTP（环回访问）可设为 `false` |
| `MUSICDL_IMAGE_TAG` | `compose.quick.yaml` 和 `compose.prod.yaml` 使用的 Docker 镜像版本 |
| `MUSICDL_AI__ENABLED` | 开启可选的 OpenAI 兼容 AI 辅助功能 |
| `MUSICDL_AI__BASE_URL` | OpenAI 兼容 API 地址 |
| `MUSICDL_AI__API_KEY` | API 凭据，不要写入版本库 |
| `MUSICDL_AI__MODEL` | 兼容端点使用的模型标识 |

面板保存的大部分配置会触发运行时热重载，不需要重启容器。涉及启动边界的设置仍可能要求重启。

如需从部署主机通过环回地址直接使用 HTTP，请将 `MUSICDL_ADMIN__COOKIE_SECURE=false`；如果前面有 HTTPS 反向代理，请保持默认值 `true`。Compose 文件默认绑定到 `127.0.0.1`，局域网访问需要另行有意修改端口绑定或反向代理配置。快速安装的覆盖命令见上文。已发布的 `1.0.1` 镜像包含此修复，原始 `v1.0.0` 镜像不支持。使用 `compose.prod.yaml` 时，请将设置写入 `.env` 并重新创建主服务；构建自定义镜像时，请使用包含 Cookie 策略修复的源码。

自定义 Compose 还需在 `musicdl.environment` 中加入 `MUSICDL_ADMIN__COOKIE_SECURE: "${MUSICDL_ADMIN__COOKIE_SECURE:-true}"`；只写入 `.env` 不会自动传给容器。升级并验证 HTTP 登录、改密成功后，可删除仅用于剥离 Secure 的临时 `panel` 反代服务及对应配置，把原入口端口直接映射到主服务的 `8000`。HTTP 会明文传输账密和会话，公网部署请使用 HTTPS。

## 音源脚本与内容责任

musicdl 的发布镜像不内置第三方音乐音源脚本。管理员可以在审查脚本分析结果和出网权限后，通过管理面板导入兼容脚本。

导入的脚本会执行第三方逻辑，并可能访问外部服务。安装前请自行检查源码、许可证、出网权限和法律风险。

本项目不授予任何受版权保护媒体的使用权。你需要自行遵守各服务提供方的条款和所在地法律。

## 安全边界

Compose 配置包含以下限制：

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

