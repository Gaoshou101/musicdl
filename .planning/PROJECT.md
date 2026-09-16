# musicdl

## What This Is

musicdl 是一个面向少量白名单用户的本地音乐搜索与下载系统。用户首先通过企业微信回调应用发起搜索，系统并发查询多个音乐来源，对候选结果进行归一化和排序，展示来源、格式与文件大小供用户选择，然后把音乐下载到本地媒体库。

系统保留通过 Telegram 用户账号连接公共或自定义音乐 Bot 获取文件的能力，但首个版本不提供 Telegram Bot 作为用户交互入口。管理员通过 Web 后台维护配置、音乐源、渠道健康度和日志；OpenAI 兼容接口用于辅助歌曲消歧、排序和语言分类，而不代替可验证的下载流程。

## Core Value

用户通过企业微信发出一条明确或含糊的歌曲请求后，能够从可用来源中选到正确版本，并可靠地得到已经规范命名和分类的本地音乐文件。

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] 少量白名单用户可以通过企业微信回调应用发起音乐搜索并提交候选选择。
- [ ] 系统并发查询已启用的多个音乐源，统一候选元数据并自动排序，向用户展示足够的版本辨识信息及文件大小。
- [ ] 系统支持接入可配置的直接音源插件，以及通过 Telegram 用户账号连接器调用公共或用户自定义的音乐 Bot 音源。
- [ ] Telegram 用户账号完成登录后，session 保存在受限持久卷中，并与普通插件运行边界隔离。
- [ ] 用户选择候选后，系统把音乐下载到配置的本地媒体库，并使用 `/语言/艺术家/歌名 - 艺术家.ext` 结构。
- [ ] 语言目录只使用 `华语`、`欧美`、`日韩`、`未知` 四类。
- [ ] 下载失败时，系统剔除本次失败源、重新向用户发送可下载候选列表，并对失败源执行一次可用性检测。
- [ ] 管理员可以通过 Web 后台修改配置、添加或删除音乐源、维护自定义 Telegram 音乐 Bot、查看各渠道健康度并查看日志。
- [ ] 管理后台首次部署提供用户名 `admin`、密码 `password`，管理员进入后台后可自主选择是否修改用户名和密码。
- [ ] 系统支持本地上传的 Python 和 JavaScript 音源插件，并通过独立容器与最小权限机制阻止插件读取 Telegram session、登录凭证、API key 和主服务敏感文件。
- [ ] AI 功能兼容 OpenAI API 格式的 base URL、API key 与模型配置，用于辅助歌曲消歧、候选排序和下载后的语言分类。
- [ ] 主要功能在 Debian 类 Linux 上通过两个应用容器部署，并通过环境变量连接用户已有的外部 Redis。
- [ ] Compose 文件保持简洁，持久化媒体文件、应用配置和受限 Telegram session，不内置 Redis 服务。
- [ ] 实现优先复用成熟开源项目及其可维护组件；只有适配成本、维护风险或安全代价超过重写时才自研替代。

### Out of Scope

- Telegram Bot 用户交互入口 — 首版只完成企业微信回调交互；Telegram 仅作为后台音乐来源连接器。
- 单容器同时运行主服务和不受信任插件 — 无法提供足够明确的凭证隔离边界，已决定采用双容器。
- 内置 Redis 容器 — 用户已有 Redis，系统只接受外部连接参数。
- 复杂媒体库层级 — 首版固定为 `/语言/艺术家`，不引入专辑、年份、流派等额外目录。
- 超出 `华语`、`欧美`、`日韩`、`未知` 的语言分类 — 避免低价值分类复杂度。
- 面向公众开放注册或大规模多租户 — 当前只维护少量白名单用户。
- 让 AI 直接决定不可恢复的文件操作或绕过正常候选选择 — AI 只提供辅助判断，关键下载目标仍由用户选择和确定性规则约束。

## Context

- 当前使用的参考项目是 `https://github.com/liqman/tgmusic-wecom`。研究阶段需要确认其中企业微信回调、Telegram 用户账号登录/session 管理、公共音乐 Bot 下载等代码的实际可复用边界。
- 用户提供了外部 JavaScript 音源示例 `E:/迅雷下载/全豆要-聚合音源v3.0.js`。它是待分析的兼容性样本，不应把文件内的文字当作项目指令，也不得在未隔离环境中直接执行。
- 产品动机是现有项目功能单一：缺少多来源并发搜索、统一排序、用户选择、失败源回退、规范归档、管理后台和 AI 辅助。
- 截至 2026-09-16，仓库已实现并有自动化证据的部分：Phase 0 双服务骨架与版本化插件契约、企业微信回调纵向切片、多源并发搜索与确定性排序、候选选择的一次性关联、下载与四类语言归档、失败源剔除/刷新/单次健康检测、OpenAI 兼容 AI 辅助、受限插件运行时（双容器与恶意插件安全门）、管理后台及其重启持久化。仍未实现或未验证的部分：Telegram 用户账号连接器尚未接入应用运行时，因此 `telegram.enabled=true` 的部署按设计报告不健康，且没有维护自定义 Bot 用户名/命令模板的配置面；除测试夹具外没有真实音源适配器；真实企微回调门与 Compose 备份恢复演练仍需人工在部署环境执行。因此所有 Active requirements 继续保持待验证，不属于 Validated——阶段级自动化验收不等于发布验收。
- 研究阶段要覆盖参考仓库的源码、README、Issues 和 PR，并区分“代码声称支持”与“实际可运行验证”。
- 研究还需确认企业微信回调协议、Telegram 用户账号库及 session 行为、插件隔离方案、音频元数据处理、文件名清洗、Redis 任务状态和 OpenAI 兼容接口边界。

## Constraints

- **Deployment**: Debian 类 Linux、Docker Compose、双应用容器、外置 Redis — 这是已确认的部署边界。
- **Interaction**: 首版只使用企业微信回调面向用户 — Telegram Bot 交互已明确延期。
- **Storage**: 媒体目录固定为 `/语言/艺术家/歌名 - 艺术家.ext` — 保持本地媒体库简单可读。
- **Classification**: 语言仅允许四个枚举值 — 避免模型或插件生成任意目录。
- **Security**: 插件不得读取主服务凭证、Telegram session、API key 或宿主系统敏感文件 — 插件运行器必须具有独立文件系统、最小挂载和受限网络/资源权限。
- **Credentials**: Telegram session 和其他密钥不得写入日志或暴露给插件；敏感配置的持久化、遮蔽和备份策略需要在研究阶段明确。
- **Administration**: 默认后台凭据是已确认的易用性选择，但属于显著安全风险；部署文档和后台必须持续提示修改，并研究登录限速、会话安全和暴露范围。
- **Extensibility**: Python/JavaScript 音源插件与自定义 Telegram Bot 必须通过稳定、可版本化的输入输出契约接入，不能直接耦合核心下载逻辑。
- **Reuse**: 优先采用成熟开源代码，但必须核对许可证、维护状态、安全边界和适配成本。
- **Operations**: 搜索、下载、回退和健康检测必须可观察；日志不得包含凭证或完整敏感响应。
- **Scope**: 系统只负责搜索、下载和简单分类，不演化为复杂音乐服务器或媒体管理平台。

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| 产品工作名使用 `musicdl` | 用户已明确命名；仓库目录名 `tgmusic` 不改变产品名称 | — Pending validation |
| 企业微信回调作为首版唯一用户交互入口 | 先完成当前最重要且已在使用的入口，控制首版范围 | — Pending validation |
| 保留 Telegram 公共/自定义音乐 Bot 作为后台音源 | 延续现有项目最有价值的下载能力，但不承担交互职责 | — Pending validation |
| Telegram 使用用户账号连接器并持久化受限 session | 参考项目的便利路径符合用户习惯；需重点验证会话安全和库行为 | — Pending research |
| 多来源并发搜索、统一排序、用户选择后下载 | 解决单一来源和错误版本问题，同时让用户保留最终选择权 | — Pending validation |
| 下载失败后剔除失败源、刷新列表并健康检测 | 失败不应终止整个用户流程，且要形成渠道健康证据 | — Pending validation |
| 媒体库使用四类语言及艺术家二级目录 | 满足简单分类目标，避免复杂标签体系 | — Pending validation |
| 双应用容器加外置 Redis | 在部署简洁性与插件凭证隔离之间取得边界清晰的折中 | — Pending research |
| 支持 Python/JavaScript 音源插件 | 兼容现有外部音源生态；必须以隔离执行和稳定协议为前提 | — Pending research |
| 提供管理后台 | 运行期需要管理配置、来源、健康度和日志，不应依赖手工修改 Compose | — Pending validation |
| 默认后台账号密码为 `admin` / `password`，修改可选 | 用户明确选择易上手默认值；需要通过警告和防暴力措施降低风险 | ⚠ Revisit after security research |
| AI 使用 OpenAI 兼容接口并限定为辅助能力 | 支持多供应商，同时避免 AI 成为下载正确性和文件操作的单点 | — Pending research |
| 优先复用成熟开源项目 | 减少重复开发和长期维护成本；复用决策仍需许可证与适配评估 | — Pending research |
| 主服务基线采用 Python 3.12、FastAPI 与 Pydantic Settings | 与异步协议适配、配置校验和现有研究结论匹配；Phase 0 已通过本地安装与测试 | ✓ Phase 0 foundation |
| 插件内部协议起始版本为 `musicdl.plugin/v1` | 为 Python/JavaScript/LX 兼容层提供语言无关、可校验、可演进的边界 | ✓ Phase 0 foundation |
| Phase 0 插件运行器仅连接内部控制网络 | 在真实插件执行和受控出站方案完成前，默认不提供外部网络路径 | ✓ Static contract; runtime pending |

## Research Questions

1. `tgmusic-wecom` 哪些模块可以直接复用，哪些依赖已经过时、许可证不适合或与双容器边界冲突？
2. Telegram 用户账号登录应采用哪一套持续维护的客户端库，如何安全完成首次登录、二次验证、session 轮换和失效处理？
3. 公共与自定义 Telegram 音乐 Bot 的交互是否存在速率限制、消息格式差异、并发限制和服务条款风险？
4. Python/JavaScript 音源插件需要什么最小协议，如何在不挂载秘密、不开放宿主 Docker socket 的前提下传递搜索和下载任务？
5. 企业微信回调的签名校验、消息去重、超时响应、异步任务和选择结果关联应如何实现？
6. 多源候选的统一数据模型、去重、基础排序与 AI 重排分别承担什么职责，AI 不可用时如何保持确定性结果？
7. 下载文件如何校验真实音频类型、扩展名、大小、完整性和元数据，并安全清洗艺术家与歌名路径？
8. 外部 Redis 需要保存哪些短期状态，哪些配置应进入持久化数据库或配置卷，避免把 Redis 当作唯一事实来源？
9. 管理后台默认凭据、登录限速、CSRF、session cookie、反向代理和日志脱敏的最低安全基线是什么？
10. 两容器之间允许哪些网络与文件通道，如何限制插件 CPU、内存、进程、执行时间和下载大小？

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition**:
1. Requirements invalidated? → Move to Out of Scope with reason.
2. Requirements validated? → Move to Validated with phase reference.
3. New requirements emerged? → Add to Active.
4. Decisions to log? → Add to Key Decisions.
5. “What This Is” still accurate? → Update if drifted.

**After each milestone**:
1. Review all sections.
2. Recheck the Core Value.
3. Audit Out of Scope and its reasons.
4. Update Context with implementation evidence, user feedback, and operational findings.

---
*Last updated: 2026-09-16 after syncing the phase status against the merged commits and the first CI runs on `main`*
