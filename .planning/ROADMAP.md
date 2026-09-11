# Roadmap

路线图采用可验收的纵向里程碑，不设日历日期。每阶段完成后由 Sol 检查真实 diff、测试证据和安全边界，再进入下一阶段。

## Phase 0 — Protocol and reuse foundation

**Goal:** 锁定参考项目可复用边界、许可证和协议事实，建立最小仓库骨架、配置模型和测试夹具。

**Dependencies:** `.planning/PROJECT.md`、研究摘要。

**Requirement IDs:** NFR-003, OPS-001。

**Exit criteria:** 参考仓库的源码/许可证可复用性有证据记录；主服务、插件服务和外部 Redis 的配置契约有版本号；敏感字段遮蔽测试通过；未验证的企微/Telegram 假设明确列出。

## Phase 1 — WeCom callback vertical slice

**Goal:** 完成白名单、企微签名验证、去重、搜索命令解析、候选选择关联和异步响应骨架。

**Dependencies:** Phase 0；企业微信官方协议探针和真实应用配置验证。

**Requirement IDs:** FR-001, OPS-001。

**Exit criteria:** 白名单用户请求可被识别，非白名单请求被拒绝；签名/解密、重复消息、超时响应和选择 token 测试通过；日志不包含密钥或原始敏感消息。

## Phase 2 — Source abstraction and deterministic search

**Goal:** 建立统一候选模型、来源注册表、并发搜索、去重、确定性排序和文件大小展示。

**Dependencies:** Phase 1；来源协议夹具。

**Requirement IDs:** FR-002, FR-003, OPS-001。

**Exit criteria:** 两个受控测试来源可并发返回统一候选；排序在重复运行中稳定；企微输出包含版本辨识字段和大小；单一来源超时不会阻塞其他来源。

## Phase 3 — Telegram user-account connector gate

**Goal:** 完成 Telegram 用户账号首次登录、2FA/失效处理、session 受限持久化，以及公共/自定义音乐 Bot 适配器。

**Dependencies:** Phase 0–2；真实 Telegram 账号、目标 Bot 行为和代理条件验证。

**Requirement IDs:** FR-003, FR-004。

**Exit criteria:** 登录成功后 session 可在重启后恢复；session 卷不挂载到插件服务；至少一个公共 Bot 和一个自定义 Bot 的命令/媒体响应有适配器测试；失效和限流状态可观察。

## Phase 4 — Download, media safety and fallback

**Goal:** 实现用户确认后的下载、音频/大小/扩展名校验、路径清洗、四类语言分类、规范命名和失败刷新流程。

**Dependencies:** Phase 2；Phase 3 至少提供一个真实或仿真可下载来源。

**Requirement IDs:** FR-005, FR-006, FR-007, OPS-001。

**Exit criteria:** 成功下载严格落在四类语言目录和标准文件名；路径穿越、伪造扩展名、超大文件和部分下载被拒绝并清理；失败源被剔除、列表刷新、一次健康检测和结果记录均可复现。

## Phase 5 — AI-assisted ranking and classification

**Goal:** 接入 OpenAI-compatible provider，提供结构化消歧/重排/语言建议，并保留确定性 fallback。

**Dependencies:** Phase 2、Phase 4；脱敏测试数据和可控 AI mock endpoint。

**Requirement IDs:** FR-011, FR-006, OPS-001。

**Exit criteria:** base URL、key、model 可配置且日志遮蔽；有效响应能影响排序或分类建议；超时、非结构化响应、无效类别和 provider 错误全部回退且不改变安全路径；AI 不可直接触发未确认下载。

## Phase 6 — Restricted plugin runtime

**Goal:** 在独立容器中支持 Python/JavaScript 插件上传、版本化 JSON 契约、资源/时间/网络限制和安全审计。

**Dependencies:** Phase 0 的契约；Phase 2 的来源抽象；Phase 4 的下载校验。

**Requirement IDs:** FR-010, FR-003, NFR-001, NFR-002。

**Exit criteria:** Python 和 JavaScript 最小插件各有契约测试；容器无 Docker socket、session/API key/主配置挂载；只读根文件系统、资源/执行时限和网络白名单生效；恶意路径和秘密读取测试被阻断。该阶段是 MVP 发布的强制安全门，未通过时不得发布 1.0。

## Phase 7 — Administration and production Compose

**Goal:** 提供管理后台、默认凭据迁移提示、来源/Bot 管理、健康度和日志查看，并完成双容器 Compose 发布基线。

**Dependencies:** Phase 1–6 中相应 API；安全评审。

**Requirement IDs:** FR-008, FR-009, NFR-001, NFR-002, OPS-001。

**Exit criteria:** admin 可完成配置和来源/Bot 增删，查看健康度与脱敏日志；默认密码登录后可修改且旧凭据失效；登录限速、CSRF、cookie 和反向代理部署检查通过；Compose 在 Debian 类环境启动两个应用容器并连接外部 Redis，不启动内置 Redis。

## Release gates

发布前必须通过：企微真实回调门、Telegram session 隔离门、媒体路径与下载完整性门、插件容器安全门、后台认证门，以及 Compose 重启/备份恢复演练。插件容器安全门是 1.0 的强制门，不得跳过；任何门未通过，相关需求保持 Active，不得仅以“服务已启动”作为验收证据。
