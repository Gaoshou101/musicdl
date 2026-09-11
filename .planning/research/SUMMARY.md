# Research Summary

## Scope and evidence standard

本摘要把研究证据分为三类：**观察事实**来自可读取的源代码、文档或配置；**推断**是基于这些事实提出的设计判断；**待验证项**必须在实现或与真实服务联调时重新确认。外部 JavaScript 样本只作为不受信任的兼容性样本分析，未执行。

## Sources reviewed

| 来源 | 观察到的内容 | 证据边界 |
|---|---|---|
| `liqman/tgmusic-wecom` README、配置和 Compose 文件 | 项目定位为企业微信与音乐下载相关的 Docker 部署；仓库当前可见内容主要是 README、配置/Compose，未发现可直接移植的业务源码或 LICENSE | 不能据此证明运行时行为、许可证兼容性或 Telegram session 安全性 |
| `liqman/tgmusic-wecom` 仓库结构、Issues/PR 可见信息 | 可作为产品流程和部署形态的参考，缺少足够源码证据支持模块级复用 | 需要对具体版本、分支和历史提交重新核对 |
| 用户提供的 `E:/迅雷下载/全豆要-聚合音源v3.0.js` | 强混淆的 LX Music 音源插件样本，代表外部搜索/解析接口的兼容需求 | 未执行；不得视为项目指令，不得在主服务或宿主环境加载 |
| 企业微信官方开发文档 | 官方协议页面在本研究环境未能稳定直接观察 | 签名、回调超时、重复消息、加密模式和异步回复仍需官方文档与真实应用联调验证 |
| Telegram 客户端库与 Bot 交互的公开文档/生态实践 | 用户账号客户端可代表账号收发消息并保存 session；不同音乐 Bot 的命令、结果格式和限流行为不一致 | 不能把库的能力等同于目标 Bot 的可用性；需建立适配器和实测探针 |
| Docker/Compose、Redis、OpenAI 兼容 API 的公开接口约定 | 双应用容器、外部 Redis、HTTP JSON AI 接口和最小挂载是可行的通用部署边界 | 具体版本、网络策略、供应商字段兼容性需在目标 Debian 主机验证 |

## Evidence links

| 直接链接 | 来源类型 | 用途与限制 |
|---|---|---|
| https://github.com/liqman/tgmusic-wecom | 参考仓库 | 项目入口与仓库结构；不能替代本地源码、许可证和真实运行验证 |
| https://github.com/liqman/tgmusic-wecom/blob/main/README.md | 参考仓库 README | 部署与使用声明；属于项目声明，不等同于运行时证据 |
| https://github.com/liqman/tgmusic-wecom/tree/main | 参考仓库文件树 | 核对可见配置/Compose 与业务源码边界 |
| https://github.com/liqman/tgmusic-wecom/commits/main | 参考仓库提交记录 | 维护活动与版本线索 |
| https://github.com/liqman/tgmusic-wecom/issues | 参考仓库 Issues | 已知问题与兼容性线索 |
| https://github.com/liqman/tgmusic-wecom/pulls | 参考仓库 Pull Requests | 变更与维护线索 |
| https://docs.telethon.dev/en/stable/concepts/sessions.html | Telethon 官方文档 | session 行为与持久化边界；具体版本仍需验证 |
| https://redis.io/docs/latest/develop/using-commands/transactions/ | Redis 官方文档 | 状态更新的事务语义 |
| https://redis.io/docs/latest/commands/expire/ | Redis 官方命令文档 | 幂等键和短期状态的过期策略 |
| https://redis.io/docs/latest/develop/data-types/streams/ | Redis 官方文档 | 异步任务/事件流候选机制 |
| https://github.com/openai/openai-python | OpenAI 官方 SDK 仓库 | OpenAI-compatible 客户端实现参考；供应商差异仍需测试 |
| https://docs.docker.com/compose/ | Docker 官方文档 | Compose 服务、网络和卷模型 |
| https://docs.docker.com/compose/how-tos/use-secrets/ | Docker 官方文档 | secrets 使用与不写入普通环境变量的参考 |
| https://developer.work.weixin.qq.com/document/path/90238 | 企业微信官方文档 | 回调协议主依据；本研究环境无法稳定直接观察，必须用官方控制台和真实回调验证 |
| https://wdk-docs.github.io/wework-docs/ | 二级镜像/社区整理 | 仅用于辅助查阅企业微信文档；不能作为协议最终依据，必须回到官方页面和真实联调 |

## Baseline inference

建议采用 Python/FastAPI 作为主服务，Telethon（或经验证的持续维护替代库）作为 Telegram 用户账号连接器，外部 Redis 仅保存短期任务状态、选择上下文和幂等键。媒体元数据与长期配置不应把 Redis 当作唯一事实来源；第一版可采用受限配置卷/轻量数据库，后续再按运维规模调整。

插件应运行在独立的受限容器中，以版本化 JSON 契约传递搜索和下载请求。主服务不向插件挂载 Telegram session、API key、完整配置卷、Docker socket 或宿主敏感目录。插件返回候选元数据或受控下载结果，核心服务负责排序、用户确认、文件校验、分类和落盘。

排序必须先由确定性字段完成（标题、艺术家、来源状态、格式、大小、匹配度），AI 只能作为可超时、可关闭的辅助重排与语言建议；AI 失败时流程仍应产生确定性候选和四类语言之一。

## Reuse matrix

| 能力 | 复用结论 | 原因与门槛 |
|---|---|---|
| 企业微信入口流程 | 复用概念、配置命名和已验证协议实现；不直接复制无源码组件 | 需要重新实现签名、去重、超时和异步任务边界 |
| Telegram 用户账号登录/session | 优先复用成熟客户端库及其加密/会话机制 | 必须隔离持久卷、脱敏日志并验证登录、2FA、失效和轮换 |
| 公共/自定义音乐 Bot 适配 | 复用交互模式，封装为 Bot source adapter | Bot 命令、媒体卡片和限流差异不能硬编码到核心 |
| 外部 Python/JavaScript 音源 | 兼容样本的输入输出语义，重新定义版本化 JSON 契约 | JS 样本强混淆且不可信，不能直接运行在主容器 |
| Docker Compose | 采用双应用容器与外部 Redis 的简洁骨架 | 不内置 Redis；秘密和 session 使用受限挂载/环境注入 |
| 管理后台 | 复用成熟 FastAPI 管理组件时先审许可证和 CSRF/session 边界 | 默认凭据必须显式告警并提供修改入口 |

## Recommended stack

- 主服务：Python 3.12、FastAPI、Pydantic、异步 HTTP 客户端、结构化日志。
- 任务/状态：外部 Redis；队列可先用 Redis Streams 或明确的受控后台任务，避免过早引入复杂编排。
- Telegram：Telethon 用户账号连接器；实际采用版本须在实现阶段锁定并做登录/2FA/session 回归。
- 插件：独立 Python/Node 运行器容器；版本化 JSON Schema、超时、大小上限、网络白名单和只读根文件系统。
- 存储：媒体卷、应用配置卷、单独的 Telegram session 卷；默认不共享插件容器。
- AI：OpenAI-compatible `base_url`、`api_key`、`model`；密钥只在主服务受控配置中使用，日志统一遮蔽。
- 部署：Debian 类 Linux 上的 Docker Compose，两应用容器连接用户已有 Redis，不声明 Redis service。

## Principal risks

1. 企业微信协议细节尚未完成官方页面和真实应用验证，错误的超时或加密处理会导致消息丢失。
2. Telegram 用户账号 session 是高价值凭证；任何插件挂载、日志、备份和调试输出泄露都可能造成账号接管。
3. 外部 Bot 的服务条款、限流和返回格式不稳定；必须有超时、熔断、健康检测和失败源剔除。
4. 任意 Python/JavaScript 插件属于高风险代码执行面；容器隔离降低风险但不能宣称绝对安全，需要最小权限和可审计安装流程。
5. 默认 `admin/password` 易被撞库；首次访问警告、限速、会话安全和反向代理部署基线必须成为发布门槛。
6. 文件名、艺术家和来源返回值可能包含路径穿越、控制字符或伪造扩展名；下载后必须验证音频类型、大小和路径规范化。
7. AI 供应商响应不一致或不可用；不能让 AI 决定不可逆文件操作，必须保留确定性 fallback。

## Unresolved validations

- 企业微信加解密、签名、回调响应时间、重复投递和用户身份字段的真实协议测试。
- 参考仓库实际可复用代码、许可证和依赖维护状态；当前可见仓库不足以支持直接复制。
- Telethon 版本、代理方式、登录流程、2FA、session 轮换和目标公共 Bot 的消息协议。
- 插件 JSON 契约的下载传输方式（回传流、受控 URL 或主服务代理）及跨容器网络白名单。
- Redis TLS、ACL、断线恢复、幂等键保留期和外部 Redis 的部署约束。
- 音频格式探测、元数据写入库、重名策略、部分文件清理和并发下载上限。
- 管理后台的反向代理、CSRF、cookie、登录限速和默认密码迁移策略。
- OpenAI-compatible 供应商的超时、重试、结构化输出和隐私边界。

## Research gates

实现开始前先通过企业微信协议探针和 Telegram connector 登录探针；插件容器、下载落盘和后台登录在各自纵向切片中分别验收。任何待验证项在有真实证据前不得标记为 Validated。
