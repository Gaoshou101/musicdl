# Phase 0 Plan — Protocol and Reuse Foundation

## Goal

建立可测试、可容器化但不连接真实企业微信、Telegram、Redis 或 AI 服务的项目基础。Phase 0 只固化配置、秘密脱敏、内部协议版本和双容器运行边界，不提前实现后续阶段业务协议。

## Scope

### Included

- Python 3.12 项目与依赖边界；
- 主服务与插件运行器的最小健康入口；
- 主服务、插件服务和外部 Redis 的版本化配置模型；
- 日志敏感字段递归脱敏；
- `musicdl.plugin/v1` 的最小请求/响应契约；
- 两应用容器、外置 Redis、最小挂载和最小权限 Compose 骨架；
- 单元测试、协议夹具与 Compose 静态检查；
- 依赖来源、许可证和延后引入理由记录。

### Deferred

- 企业微信验签、解密、白名单和消息发送；
- Telegram 登录、session 创建和音乐 Bot 交互；
- Redis 队列或状态读写；
- 真实音源、下载和媒体落盘；
- OpenAI-compatible API；
- 上传并执行第三方插件；
- 管理后台业务功能。

## Execution units

### Unit 1 — Repository and dependency baseline

- 创建 `pyproject.toml`、`.gitignore` 和第三方依赖记录；
- 固定 Python 3.12 与已核验的直接依赖版本；
- 创建本地虚拟环境并验证依赖解析。

### Unit 2 — Test-first configuration and contracts

- RED：先为配置版本、危险默认值、秘密脱敏和插件协议编写失败测试；
- GREEN：实现最小 Pydantic 配置、脱敏函数和协议模型；
- REFACTOR：只消除测试已覆盖的重复，不添加业务行为。

### Unit 3 — Services and deployment boundary

- RED：先编写主服务/插件服务健康入口及 Compose 安全边界测试；
- GREEN：实现最小 FastAPI 入口、Dockerfile、Compose 和示例环境变量；
- 静态验证 Compose 只包含两个应用服务且不声明 Redis 服务；
- 在可用 Docker 环境执行构建与 `docker compose config`。当前 Windows 主机未发现 Docker，因此该项必须保留为目标 Debian 主机验证门。

## Acceptance

- 所有新增行为都有先失败后通过的测试证据；
- `pytest` 完整通过；
- 配置和协议包含明确版本；
- 测试证明秘密不会出现在模型展示或结构化日志中；
- Compose 不包含 Redis 服务，插件容器不挂载 Telegram session 或主服务秘密；
- 没有真实凭证、session、API key 或外部插件代码进入仓库；
- Sol 审查完整 diff 和测试结果后才可提交并推送。

## Rollback

每个执行单元使用独立提交。失败时使用新的 Git revert 提交回滚对应单元，不使用 `git reset --hard`，不修改既有规划和 Codex 配置。
