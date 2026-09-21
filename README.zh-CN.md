<h1 align="center">musicdl</h1>

<p align="center">面向少量企业微信白名单用户的本地多音源音乐搜索与下载服务：每一次下载都会按四类语言归档到本地媒体库，也可以用自带的管理后台直接驱动。</p>

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

## 核心亮点

| 亮点 | 对使用者的价值 |
|---|---|
| 搜索与选择都在企业微信里完成 | 用户发送 `/search <关键词>` 或直接发送文本，再回复序号即可完成选择。命令解析位于 `musicdl.wecom.commands`，一次选择绑定一份不可变的候选快照。 |
| 所有已启用音源并发查询 | `search_sources` 按单个音源的超时并发调用，再依据注册表优先级、无损格式、码率、元数据完整度与文件大小去重排序。 |
| 下载先校验再入库 | 容器格式由响应字节重新判定，因此把 FLAC 流标成 `audio/mpeg` 的上游会被识别出来，而不是被直接信任。文件落在 `<媒体根目录>/<分类>/<艺术家>/<歌名> - <艺术家>.<扩展名>`。 |
| 单个音源失败不会中断流程 | worker 会对该音源做一次健康检测、持久化刷新后的候选列表，并重新发送一次选择提示；系统不会自行下载替代文件。 |
| 不可信音源插件拿不到任何凭证 | `plugin-runner` 没有 Redis 地址、没有 Telegram session、没有 API key、没有 Docker socket，只接入内部的 `plugin-control` 网络；Python 插件还额外受 seccomp 与资源限制约束。 |
| AI 只做辅助，且默认关闭 | `MUSICDL_AI__ENABLED=false` 时排序与语言分类完全确定；即使开启后模型不可用，也会回落到相同的确定性结论。 |
| 后台自己就能验证音源 | `GET /admin/search`、`POST /admin/download` 与 `GET /admin/media/{path}` 跑的是 worker 用的同一个注册表，不必先给机器人发消息就能确认音源可用。 |
| 依赖健康度区分“无事可做”与“真的坏了” | `/admin/health` 会给出 `ok`、`failed` 或 `not_required`；在当前部署里本就无事可做的依赖不会把正常工作的后台一直标红。 |
| 运行配置在后台就能改，改完即生效 | `GET /admin/config` 与 `PATCH /admin/config` 让企微、Redis、Telegram、AI 与任务预算在面板里改完即生效，不必回到宿主机改 `.env` 再重建容器。面板拥有的每一项都在原地生效：保存会按当前配置重建运行环境、替换它的 worker，并把这次重建的结果回报给页面，所以换一个凭证、改一个预算或增删一个 Bot 都不必重启。密钥只写不读，只有 compose 能改的项会被如实标注归属。 |
| 渠道健康度回答“哪个音源坏了” | `GET /admin/sources/health` 把后台自己发起的搜索与下载汇总成每个音源的结论（正常 / 不稳定 / 异常 / 未验证）、最近若干次结果的成功率与最后一次报错。依赖健康度说的是 Redis 通不通，它说的是某一个渠道还能不能用。 |
| 一个渠道坏掉不再拖垮整个后台 | 后台自己的搜索在音源条件相同时，优先选择被观测到还能用的渠道，而不是按字母序碰运气；下载失败后会按同一次搜索的关键词刷新一次候选，并在另一个渠道找到同一首歌时只重试一次。 |
| 后台能直接读到服务自己的日志 | `GET /admin/logs` 返回本进程内存里最近的日志行（带级别、logger、消息与异常栈），面板的「日志查看」页可以直接按级别过滤、按游标增量拉取；URL 上的查询串与常见凭证字段在进入窗口前就被脱敏。 |

## 架构

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
                                                       │  /wecom/callback、控制台与 /admin
                                                       ▼  是同一个进程：http://musicdl:8000
│            musicdl main service   (uid 10001, read-only rootfs)            │
│                                                                            │
│┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐│
││       FastAPI        │  │    MessageWorker     │  │      JobWorker       ││
││    /wecom  /admin    │  │    search + rank     │  │   resolve+publish    ││
││  /wecom  /admin  /   │  │    search + rank     │  │   resolve+publish    ││
││  + static console    │  │                      │  │                      ││
           │                         │                         │
           HTTP                      redis://                  HTTPS
           ▼                         ▼                         ▼
┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│    plugin-runner     │   │   Redis (external)   │   │  kw / wy / tx / kg   │
│   internal network   │   │  jobs, dedup, state  │   │ + media hosts (TLS)  │
└──────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

`compose.yaml` 运行两个应用服务，不包含 Redis。`musicdl` 服务持有媒体、应用数据与 Telegram session 三个卷，并从 `/` 提供控制台——这些静态文件由 `docker/main/Dockerfile` 构建进它自己的镜像；`plugin-runner` 只接入内部的 `plugin-control` 网络，且不接收任何密钥。控制台是静态文件而不是第二个运行时：线上没有为它跑的 Node 进程，没有第二个对外端口，也没有一条路径可能和 API 走偏的重写规则。Redis 始终是外部服务，通过 `MUSICDL_REDIS__URL` 配置。

## 使用示例

进入企业微信回调的每条消息都会被解析成一条命令。直接发送文本等同于一次查询，所以 `月光` 与 `/search 月光` 是同一个请求。

| 消息 | 边界行为 |
|---|---|
| `/search <关键词>` 或直接发送文本 | 入队一次搜索。消息 worker 查询已启用音源、排序结果，并回复带序号的列表。 |
| `1` 到 `100` | 入队对该候选的选择。任务 worker 解析媒体描述、流式下载并校验字节，最后发布文件。 |
| `n`、`p`、`/c`、`/cancel` | `parse_command` 能识别并入队，但目前没有 worker 消费它们，因此不会产生回复。 |
| 其他 `/命令` | 记录为不支持。 |

回复中的每一行对应一个候选，音源提供专辑、时长或码率时会追加显示：

```text
1. <title> - <artist>（<source_id>@<source_version>；格式 <format>；大小 <size>）

回复序号下载。
```

`musicdl` 服务发出这段文本，用户回复 `1` 完成选择。选中的候选绑定到某一代快照，因此针对旧候选列表签发的令牌无法选中刷新后的候选。

### 不依赖企业微信验证一个音源

后台跑的是与 worker 相同的注册表，因此在任何企业微信或 Telegram 账号存在之前就能先把音源验证一遍。Cookie 带 `Secure` 标记，所以要经 HTTPS 访问，并复用登录返回的 Cookie 文件与 `csrf_token`：

```bash
curl -s -c cookies.txt -H 'content-type: application/json' \
  -d '{"username":"admin","password":"password"}' \
  https://<panel-host>/admin/login

curl -s -b cookies.txt 'https://<panel-host>/admin/search?q=<query>'

curl -s -b cookies.txt -H "x-csrf-token: <csrf_token>" -H 'content-type: application/json' \
  -d '{"candidate": <one candidate from the search reply>}' \
  https://<panel-host>/admin/download
```

登录返回 `{"ok":true,"must_change":true,"csrf_token":"…"}`，搜索返回候选列表以及每个音源一条状态，下载返回相对路径、字节数、媒体类型与 SHA-256；随后 `GET /admin/media/<相对路径>` 会把该文件回放出来，浏览器可以直接播放。

## 快速安装

```bash
git clone https://github.com/Gaoshou101/musicdl.git
cd musicdl
cp .env.example .env
# Set MUSICDL_REDIS__URL in .env to your own external Redis endpoint.
docker compose build
docker compose up -d
```

前置条件：

- Debian 类 Linux 主机，并安装带 Compose 插件的 Docker Engine。
- 一个可从 `musicdl` 容器访问的外部 Redis。Compose 不会启动 Redis。
- 只有在需要运行测试时才需要 Python 3.12 与本地虚拟环境。

仓库 `Gaoshou101/musicdl` 目前是私有仓库，克隆它需要相应访问权限。

## 快速开始

```bash
docker compose ps
curl http://127.0.0.1:${MUSICDL_PORT:-8000}/healthz
```

整个门户只用一个端口：`/` 是控制台，`/admin/*` 是它调用的接口，`/wecom/callback` 是回调边界。`/wecom` 以上的部分都属于同一个进程，所以没有第二个健康探测地址，也不会出现「控制台说自己健康、应用已经挂了」这种情况。

`/healthz` 返回 `{"service":"musicdl","status":"ok","config_version":1}`，并且刻意不访问 Redis 与插件运行器，因此它只能证明进程存活。就绪状态是分开的：开启企业微信时 `/readyz` 会探测由 Redis 支撑的状态；关闭时虽然没有 worker 会掉线，但若后台检索所用的音源运行时未能装配，`/readyz` 仍会返回 `503`。

从服务跑起来到真正可用还需要三步：

1. 通过 HTTPS 反向代理访问 `/admin/`，或走浏览器视为可信主机的回环隧道。所有 Cookie 都带 `Secure` 标记，明文 HTTP 无法进入后台。未登录访问会直接收到登录页本身；在改掉默认凭证之前，除 `/admin/`、`/admin/change-credentials` 与 `/admin/change-credentials-form` 之外的每个路由都会返回 `403`。
2. 通过 `POST /admin/sources` 安装音源插件，或通过 `POST /admin/bots` 定义一个 Telegram Bot，然后在后台搜索并下载一次，确认该音源确实可用。没有任何已启用音源的部署没有可搜索的内容。镜像自带 `music_v1bot` 定义（默认命令 `/search {query}`），所以新装的部署不用先定义 Bot 就有这一条；不想用它就在 Bot 管理页改名、禁用或删除，删掉之后重启也不会复活。Bot 背后还需要一个已授权的账号：`api_id` 与 `api_hash` 只是把客户端接好，首次登录要在 Bot 管理页的登录卡片里完成（手机号、Telegram 发来的登录码，账号有两步验证时再补一次密码），否则页面显示「未登录」，而每次搜索都会以 `invalid_session` 失败。
3. 用 `MUSICDL_WECOM__ENABLED=true` 以及下表中的凭证开启企业微信边界，然后在企业微信中发送 `/search <关键词>`。这个边界是可选的：没有它后台照样能用。

## 配置

所有配置项都使用 `MUSICDL_` 前缀，并以 `__` 作为嵌套分隔符。把 `.env.example` 复制为 `.env`，只替换占位值；不要提交 `.env` 或真实凭证。

下表中的一部分也可以交给后台：面板把自己的覆盖层写进状态文件，并在启动时叠在部署的环境变量之上。面板只认识自己声明过的那批字段，每项都带着当前来源（面板覆盖、部署变量或默认值）。面板拥有的每一项都是立即生效：只有真正改动到运行环境会读的字段时才会重建一次，并回报这次重建产生的第几代运行环境；端口、卷挂载、资源上限这类只有 compose 能改的项会单独列出并注明应改的变量名，而不是被伪装成能在面板里修改的开关。

| 变量 | 是否必需 | 用途 |
|---|---|---|
| `MUSICDL_REDIS__URL` | 必需 | 外部 Redis 地址，支持 `redis://` 与 `rediss://`。 |
| `MUSICDL_PORT` | 可选 | Compose 发布的回环端口，默认 `8000`。 |
| `MUSICDL_ADMIN__PANEL_ROOT` | 可选 | 控制台静态导出在容器内的位置。应用镜像已设为 `/app/panel`；没有构建过控制台的检出不会在 `/` 提供首页，只记一条警告，而不是拒绝启动。 |
| `MUSICDL_WECOM__ENABLED` | 需要对外服务时 | 开启回调边界，默认 `false`。 |
| `MUSICDL_WECOM__CORP_ID`、`__AGENT_ID`、`__TOKEN`、`__SECRET` | 开启企业微信时 | 企业 ID、应用 AgentId、回调 Token 与应用 Secret。 |
| `MUSICDL_WECOM__API_BASE` | 可选 | 出网 API 地址，默认官方 `https://qyapi.weixin.qq.com`。海外部署可指向国内的反向代理（例如 `ddsderek/wxchat` 的 `http://主机:9080`），这样 `gettoken` 与发消息都从可信 IP 发出；注意公网明文 HTTP 会让应用 Secret 暴露在链路上。 |
| `MUSICDL_WECOM__ENCODING_AES_KEY` | 开启企业微信时 | 43 位 EncodingAESKey，解码后必须是 32 字节。 |
| `MUSICDL_WECOM__ALLOWED_USERS` | 开启企业微信时 | 白名单；为空时启动失败。 |
| `MUSICDL_ADMIN__ENABLED` | 可选 | 是否挂载 `/admin`，默认 `true`。 |
| `MUSICDL_ADMIN__STATE_PATH` | 可选 | 后台状态文件的绝对 POSIX 路径；Compose 设为 `/data/app/admin-state.json`。 |
| `MUSICDL_ADMIN__LOGIN_LIMIT`、`__LOGIN_WINDOW_SECONDS` | 可选 | 登录失败预算，默认每 `60` 秒 `5` 次。 |
| `MUSICDL_PLUGIN__SERVICE_URL` | 由 Compose 设置 | 插件运行器内部地址 `http://plugin-runner:8080`。 |
| `MUSICDL_PLUGIN__APP_DATA_ROOT` | 可选 | 已安装插件代码的存放位置，默认 `/data/app`。 |
| `MUSICDL_MEDIA__ROOT` | 由 Compose 设置 | 主服务的媒体挂载点 `/data/music`。 |
| `MUSICDL_TELEGRAM__ENABLED` | 可选 | 注册 Telegram 连接器与后台维护的 Bot 定义，默认 `false`；连接器未开启时内置的 `music_v1bot` 定义仍会出现在后台，但不会被注册成音源。 |
| `MUSICDL_TELEGRAM__API_ID`、`__API_HASH` | 开启 Telegram 时 | Telegram 应用凭证。 |
| `MUSICDL_TELEGRAM__PROFILE`、`__SESSION_ROOT` | 可选 | 受限 session 的档案名与挂载点，默认 `default` 与 `/data/telegram-sessions`。 |
| `MUSICDL_AI__ENABLED` | 可选 | 辅助排序与语言分类，默认 `false`。 |
| `MUSICDL_AI__BASE_URL`、`__API_KEY`、`__MODEL`、`__TIMEOUT`、`__MAX_CANDIDATES` | 开启 AI 时 | OpenAI 兼容接口配置。 |
| `MUSICDL_WORKER__*` | 可选 | 搜索、解析、健康检测与任务预算；这些设置会在启动时自我校验相互不等式，而不是等到第一次使用才报错。 |

## 管理后台

后台挂在 `/admin`，并且可以独立使用：只要企业微信 worker 或后台其中之一启用，它检索所用的音源注册表就会被装配起来，因此没有任何企业微信账号的部署同样能得到一个可用的注册表，既不需要 Redis，也不需要企业微信客户端。

| 路由 | 用途 |
|---|---|
| `GET /admin/` | 控制台首页。没有会话的浏览器会直接收到登录页本身。 |
| `POST /admin/login-form` | 登录页自身的提交：进来一页、出去一次跳转，与 JSON 登录共用限速与审计记录。 |
| `POST /admin/login` | 带限速的 JSON 登录，签发会话与 CSRF Cookie，并返回令牌。 |
| `POST /admin/change-credentials-form` | 默认凭证仍然生效时，控制台首页上负责改用户名与密码的表单。 |
| `POST /admin/change-credentials` | 同样改动的 JSON 版本。 |
| `GET /admin/search?q=` | 复用 worker 所用的同一个 `search_sources`，返回候选列表以及每个音源一条状态。 |
| `POST /admin/download` | 把搜索返回的某个候选下载进媒体根目录，返回相对路径、字节数、媒体类型与 SHA-256。 |
| `GET /admin/media/{path}` | 回放已下载的文件，任何没有落在媒体根目录之下的路径都会被拒绝。 |
| `GET /admin/sources`、`POST /admin/sources` | 列出并安装音源插件。 |
| `POST /admin/sources/analyze` | 预览一次导入且不写入任何内容。 |
| `PATCH /admin/sources/{id}`、`DELETE /admin/sources/{id}` | 启用、调整优先级或删除音源。 |
| `GET /admin/bots`、`POST /admin/bots`、`PATCH /admin/bots/{id}`、`DELETE /admin/bots/{id}` | 维护会成为搜索源的 Telegram Bot；没有存过 Bot 列表的部署从内置的 `music_v1bot` 开始。 |
| `GET /admin/telegram` | 已存 Telegram 会话自身的状态（`ready`、`invalid_session`、`code_required`、`password_required`、`rate_limited`、`error`），以及未完成登录正在等待的那个打码手机号。 |
| `POST /admin/telegram/login`、`POST /admin/telegram/login/verify`、`POST /admin/telegram/login/password` | 一次性登录的三步：向手机号发送登录码、提交登录码，账号有两步验证时再提交密码。 |
| `POST /admin/telegram/logout` | 删除已存的会话，下次登录从新的登录码开始。 |
| `GET /admin/health` | 汇总依赖健康度。 |
| `GET /admin/config`、`PATCH /admin/config` | 读出面板能改的每一项、它当前的来源与生效值（密钥只回答“是否已设置”），并按批校验后保存一组改动；写入会在原地被采用，答复里用 `reload` 说明这次重建的结果（`reloaded`、`failed` 或 `skipped`）。 |
| `GET /admin/sources/health` | 每个音源最近若干次搜索与下载的成败汇总，附最后一次报错的阶段与代码。 |
| `GET /admin/events`、`GET /admin/audit` | 分页事件与审计日志。 |
| `GET /admin/logs` | 本进程内存里的服务日志窗口：游标（`after`）增量读取，可按 `level` 过滤，回答的是“服务当时打印了什么”，而不是面板自己问过什么。 |

JSON 路由继续返回 JSON，因此未认证的 `GET /admin/sources` 仍然是 `401`；两个 HTML 表单则把会话的 CSRF 令牌放进隐藏字段双提交，因为表单提交无法设置 API 路由所用的 `x-csrf-token` 头。表单体在进程内解析（`musicdl.admin.forms`），不走 `request.form()`，因此只为读两个字符串并不会让 `python-multipart` 变成运行时依赖。

`GET /admin/health` 汇报四项检查——`readyz`、`redis`、`plugin_runner` 与 `telegram`——每一项都有 `ok`、`failed`、`not_required` 三种结论。没有企业微信账号时 Redis 是 `not_required`，运行时还没需要插件运行器时它也是 `not_required`，因此两者都不会让一个正常工作的部署一直标红。Telegram 的就绪状态在这里汇报而不是放进 `/readyz`：开启后，只有在连接器已接线、至少注册了一个 Bot 定义、且 session 恢复为就绪时才是 `ok`，否则是 `failed`；关闭时则是 `not_required`。

渠道健康度不做任何主动探测，它只汇总后台自己发起的搜索与下载：在「搜索测试」里跑一次，所有启用的音源都会得到一行；下载则补上搜索答不了的那一问——这个渠道到底能不能把音频取回来。结论按每个音源最近 20 次结果计算：全部成功是「正常」，最近一次失败是「异常」，失败之后又成功是「不稳定」，一次都没跑过则是「未验证」——没有证据不等于故障。已经被移除但留下过记录的音源仍会列出并标记「已移除」，因为刚删掉一个音源时，恰恰是它最后的报错最值得看一眼；记录只保留最近的固定窗口，占用与流量无关。

所有写操作都需要双提交 CSRF 令牌。状态写入 `MUSICDL_ADMIN__STATE_PATH` 并在启动时重新加载，因此改动过的音源、Bot 与密码都能在重启后保留，并且已存储的条目优先于运行时再次发布的值。状态文件不可读或版本不受支持时会直接中止启动，而不是悄悄恢复默认密码；写入失败会回滚内存中的改动。

同一份状态文件也保存面板自己的配置覆盖层（`settings` 键）：改过的字段记在这里，没改过的仍由部署的环境变量决定，两者都没有的才回落到默认值。一批改动整体校验、整体落盘，任何一项非法都会让整批被拒绝，不会只保存一半；重启后如果某项在新版本里不再合法，它会被丢弃并在面板上列出来。密钥从不回传，留空表示保持原值，只有显式的 `null` 才会清除覆盖、回落到部署值。

改这些都不再需要重启。只要一次保存动到了运行环境真正会读的字段——企微凭证或消息代理、Telegram Bot 列表、Redis 地址、worker 预算——就会在原地重建运行环境：下一代运行环境是在**动到**当前这一代之前先装配好的，所以装配失败时正在跑的部署毫发无伤；被替换掉的那一代的 worker 会被取消，新的 worker 接上，而每个写操作的答复都会带上 `reload`（成功时是 `reloaded` 加代数与渠道数，失败时是 `failed` 加原因，没碰到运行环境时则不带这个字段）。被取消的 worker 会把消息留在 Redis 流里，下一代运行环境的 `XAUTOCLAIM` 会重新捡起来，所以一次热重载的代价是一次重试而不是一条消息：改完凭证后 `docker ps` 里的 `Up` 时间不变。

## Web 控制台

`web/` 是一个可选的 Next.js 控制台，只调用上面的这些路由，包含仪表板、音源管理、Bot 管理、运行配置、健康监控、日志与设置七个页面。它全部是客户端代码，所以 `next build` 直接导出静态文件，由应用从 `/` 提供，并就在同一个来源上调用 `/admin/*`——没有需要维护的重写规则，也没有构建期要固定的后端地址；「运行配置」页同时展示面板能改的字段与只有 compose 能改的容器项，改动会先攒成一批再一起保存。

Bot 管理页上还带着这些定义实际调用所用的 Telegram 账号：会话自身的状态、三步完成的首次登录（手机号、登录码、两步验证密码），以及一个删除已存会话的退出登录。Bot 定义回答的是「要问哪个 Bot」，而这张卡片才能让提问真的发生。

```bash
cd web
npm install
npm run build
```

`npm run check:api:remote` 对已部署的控制台跑只读契约检查，`npm run check:api:local` 对本机应用跑完整流程。控制台不是独立服务：`docker/main/Dockerfile` 把它构建进应用镜像，应用从 `/` 提供它，因此只要让应用的 `MUSICDL_ADMIN__PANEL_ROOT` 指向 `web/out` 就够了；目录结构见 `web/STRUCTURE.md`。

## 音源插件

清单文件声明语言、操作、源码摘要以及该音源可以访问的主机：

```json
{"plugin_id":"example","version":"1","language":"python",
 "operations":["search","resolve"],"allowed_hosts":["api.example.com"],
 "sha256":"<64 lowercase hex characters>"}
```

四项按音源授予的权限属于运维决策，而不是脚本可以自行声明的事实：`allowed_ports`、`allow_insecure_http`、`allow_ip_hosts`、`allow_any_host`。最后一项最宽松也最缺乏保护。每一项被授予的放宽都会在 `plugin.egress` 中返回，运维可以在安装前后准确读到当前生效的内容。

插件入口是 `handle(request)` 函数，可以返回与 JSON 兼容的结果，也可以一次返回一个 HTTP `GET`/`POST` 动作；动作可以指定请求头，并可为 `POST` 指定 base64 请求体，但不能指定请求行、`Host` 或任何分帧头。

边界是固定的，而不是按插件可配置的：

| 面向 | 上限 |
|---|---|
| 插件源码 | 256 KiB |
| 载荷与结果 | 64 KiB |
| 单次代理 HTTP 观测 | 1 MiB |
| 单个任务的 HTTP 动作数 | 8 |
| 发布的媒体文件 | 500 MiB |
| 任务墙钟时间、CPU 时间 | 30 秒、5 秒 |
| Python 地址空间、Deno 堆 | 256 MiB、128 MiB |

传输由主进程负责。`SecureMediaTransport` 会对主机名做 IDNA 归一化、自行解析 A 与 AAAA 记录、拒绝所有非全局地址，然后连接到固定的数值地址，同时在 TLS SNI、证书校验与 `Host` 头中保留已批准的主机名。它不携带环境中的任何凭证、忽略代理设置、从不跟随重定向，并在结束后关闭 socket。

有些限制是刻意为之：插件不能导入任意包、读取环境变量或密钥、持久化文件、使用任意 URL、重定向或代理、在 URL 中携带凭证，也不能直接调用主服务。

## 发布门禁

`scripts/release/run_gates.py` 决定发布门禁。默认的 `--gate all` 把 `NOT_RUN` 视为失败，因此无法执行的门禁绝不会被报告为通过。`--dry-run` 打印同样的表格，但不会因为需要真实部署的门禁而失败；`--self-test` 会先篡改 Compose、Telegram、代理与插件边界，再打印表格。

| 门禁 | 它判定什么 |
|---|---|
| `compose` | 两个应用服务、无 Redis 服务、非 root 用户、只读根文件系统、硬性 CPU 与内存上限、健康探测与内部插件网络。 |
| `proxy` | Nginx 与 Caddy 模板是否终止 TLS、保留回调查询串，并把回调排除在访问日志之外。 |
| `wecom-callback` | 位于公网 HTTPS 之后的真实企业微信应用；需人工执行，配合 `--wecom-url` 与 `--wecom-expected`。 |
| `telegram-session-isolation` | session 卷及其凭证只属于主服务。 |
| `media-integrity` | 媒体相关测试文件在 `-W error` 下通过。 |
| `plugin-security` | 静态边界成立，并且强制性的恶意运行时套件 `tests/integration/test_plugin_runtime.py` 在真实 Docker Engine 上无跳过地通过。 |
| `admin-auth` | 后台相关测试文件在 `-W error` 下通过。 |
| `compose-recovery` | 重启与卷契约成立、备份脚本能解析每个卷，并在隔离的 Redis 上完成一次恢复演练。 |

CI（`.github/workflows/ci.yml`）会在每次 push 与 pull request 上运行测试套件与 `--self-test --dry-run`。托管运行器自带 Docker Engine，因此 `plugin-security` 会在那里真实执行；`wecom-callback` 与 `compose-recovery` 始终由人工执行，因为没有托管运行器能诚实地判定它们。另有一个 `admin-panel` 任务会对控制台做类型检查，然后**用 `compose.prod.yaml` 本身**构建并启动应用服务——就是主机上那份只读根文件系统、那些卷和那条命令——再向它要 `/healthz`、控制台首页，以及 `/admin/sources` 的 `401`。在 workflow 里复述这套边界只能证明 workflow 自己写的参数，驱动 compose 才能证明真正发布出去的那个容器。

## 开发

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q -W error
```

测试套件由 57 个单元测试文件与 3 个集成测试文件组成。其中两个会自行判断能否运行：`tests/unit/test_worker_redis_live.py` 在未设置 `MUSICDL_TEST_REDIS_URL` 时跳过，`tests/integration/test_plugin_runtime.py` 在没有 Docker Engine 时跳过。只有同时具备 Docker 与测试 Redis 的主机才能给出完整答案。

`web/` 中的控制台有自己的工具链：`npm run build` 构建它，`npm run check:api:remote` 对已部署面板检查契约，`npm run check:api:local` 对本机应用与面板跑完整流程。

## 文档

| 主题 | 内容 | 链接 |
|---|---|---|
| 部署环境 | Compose 必需的外部 Redis 地址，以及可选的 AI 配置块 | [.env.example](./.env.example) |
| 运行时配置 | 每个配置分组及其默认值，集中在同一个模型里声明 | [config.py](./src/musicdl/config.py) |
| 部署形态 | 服务、卷、网络与安全选项 | [compose.yaml](./compose.yaml) |
| 生产限制 | 每个服务的 CPU、内存与重启策略 | [compose.prod.yaml](./compose.prod.yaml) |
| 反向代理 | 位于后台之前终止 TLS 的 Nginx 与 Caddy 模板；所有请求都只发往一个上游，因为控制台与它背后的接口是同一个进程 | [deploy](./deploy) |
| 控制台构建 | 控制台如何被编译进应用镜像，以及静态导出落在哪里 | [docker/main/Dockerfile](./docker/main/Dockerfile) |
| 控制台结构 | 面板的目录结构与契约检查命令 | [web/STRUCTURE.md](./web/STRUCTURE.md) |
| 发布门禁 | 门禁脚本、篡改自检与备份演练 | [run_gates.py](./scripts/release/run_gates.py) |
| 依赖清单 | 固定版本、许可证与供应链证据 | [THIRD_PARTY.md](./THIRD_PARTY.md) |

本仓库目前没有许可证文件，因此本文不对许可证作任何声明。在发布或再分发代码之前，请先补充 `LICENSE` 文件。

