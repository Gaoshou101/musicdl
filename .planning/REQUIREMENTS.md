# Requirements

每条需求均有稳定 ID。`MVP` 表示首个可用版本必须交付。所有需求均来自 `.planning/PROJECT.md` 的 Active 列表；每条 Active 需求在本表只映射一次。

## Functional requirements

| ID | 来源映射 | 优先级 | 验收标准 |
|---|---|---|---|
| FR-001 | Active 1：白名单与企微搜索/选择 | MVP | 配置的少量用户可通过企微身份发起搜索；非白名单请求被拒绝；候选选择可关联到原请求并只执行一次。 |
| FR-002 | Active 2：多源并发、归一化、排序、文件大小展示 | MVP | 至少两个启用来源并发查询；结果拥有统一字段、去重和稳定排序；企微消息展示来源、标题、艺术家、格式和大小。 |
| FR-003 | Active 3：直接音源插件与 Telegram Bot 来源 | MVP | 启用的直接插件和 Telegram 用户账号 Bot 适配器都能返回统一候选；来源可单独启停。 |
| FR-004 | Active 4：Telegram 登录与受限 session | MVP | 完成登录后 session 写入独立持久卷；主服务可用但插件容器无该卷和 session 内容访问权。 |
| FR-005 | Active 5：下载、分类与命名 | MVP | 用户确认后下载到配置媒体库，路径严格为 `/<语言>/<艺术家>/<歌名> - <艺术家>.<ext>`；重名和非法字符有确定性处理。 |
| FR-006 | Active 6：四类语言目录 | MVP | 目录值只能是 `华语`、`欧美`、`日韩`、`未知`；AI、插件或异常输入都不能生成其他目录。 |
| FR-007 | Active 7：下载失败 fallback | MVP | 下载失败源从当前候选剔除；用户收到刷新后的候选列表；失败源随后执行一次可用性检测并记录结果。 |
| FR-008 | Active 8：管理后台 | MVP | 管理员可修改配置、增删来源和自定义 Telegram Bot，查看来源健康度与日志；变更在重启后保持。 |
| FR-009 | Active 9：默认后台凭据与修改入口 | MVP | 首次部署可用 `admin/password` 登录并显示安全提示；后台提供修改用户名和密码功能，修改后旧凭据失效。 |
| FR-010 | Active 10：Python/JavaScript 插件隔离 | MVP | 上传的 Python/JS 插件在独立容器按版本化契约运行；测试证明其不能读取 session、API key、主服务秘密或宿主敏感文件。 |
| FR-011 | Active 11：OpenAI-compatible AI 辅助 | MVP | 可配置 base URL、API key、模型；AI 可辅助消歧、排序和语言分类；超时、错误或关闭时自动回到确定性逻辑。 |

## Non-functional requirements

| ID | 来源映射 | 优先级 | 验收标准 |
|---|---|---|---|
| NFR-001 | Active 12：Debian/Docker 双应用容器/外部 Redis | MVP | 在目标 Debian 类 Linux 通过 Compose 启动主服务和插件服务；Compose 不创建 Redis，二者按环境参数连接外部 Redis。 |
| NFR-002 | Active 13：简洁 Compose 与持久化 | MVP | Compose 只声明必要服务、网络、卷和健康检查；媒体、应用配置、Telegram session 使用明确持久化卷，升级后数据可恢复。 |
| NFR-003 | Active 14：优先复用成熟开源组件 | MVP | 每个关键依赖有许可证、维护状态和适配成本记录；直接复用优先，重写需记录超过适配成本/风险的理由。 |

## Operational requirements

| ID | 来源映射 | 优先级 | 验收标准 |
|---|---|---|---|
| OPS-001 | PROJECT Context/Constraints 中的可观察性、凭证与运维约束 | MVP | 搜索、下载、fallback、健康检测和管理操作输出结构化日志与状态；秘密、session、API key 和完整敏感响应不会写入日志；失败可定位到请求、来源和阶段。 |

## Security requirements

安全约束是对上述功能的验收门槛，不新增 PROJECT.md Active 条目：插件不得读取主服务秘密；Telegram session 与 API key 不进日志；媒体路径和下载内容必须校验；后台登录必须限速并使用安全会话；跨容器权限保持最小化。若任一安全门槛不满足，对应功能不得验收为完成。

## Traceability

PROJECT Active 1–11 → FR-001–FR-011；Active 12–14 → NFR-001–NFR-003。OPS-001 是由 PROJECT Context/Constraints 中的可观察性、凭证和运维约束提炼的跨条目验收门槛，不伪造为 Active 条目。PROJECT 的安全、凭证、部署、扩展和 AI 限制属于跨条目约束，已在 Security requirements 和对应验收标准中覆盖，不重复创建需求 ID。
