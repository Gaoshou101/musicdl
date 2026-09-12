# Phase 1 Plan — WeCom Callback Vertical Slice

## Goal

交付企业微信自建应用加密回调的最小安全纵向切片：验证 URL、验签并解密消息、按成员白名单授权、原子去重并投递异步任务、解析搜索/选择命令，并提供绑定用户且只能消费一次的短期选择上下文。Phase 1 不连接真实音乐源，也不下载文件。

## Evidence and reuse decision

- 企业微信协议以官方文档路径 `90238` 及腾讯团队 `weworkapi_python` 示例交叉核对；官方页面在当前工具中无法稳定打开，因此真实应用联调仍是验收门。
- `liqman/tgmusic-wecom` 当前公开分支只有 README、配置和 Compose，没有可审查业务源码或 LICENSE。仅参考 `/wecom/callback`、文本搜索和数字选择的交互语义，不复制代码或部署结构。
- 腾讯 Python 示例使用历史 PyCrypto/Python 兼容代码，且仓库明确称其仅为示范。生产实现使用维护中的 `cryptography`，只实现企微规定的窄协议接口。
- 外部 Redis 使用维护中的 `redis-py` asyncio 接口；Compose 继续不内置 Redis。

## Security decisions

- 仅接受加密模式；签名必须在 AES 解密与 XML 业务解析之前通过，并用常量时间比较。
- `EncodingAESKey` 必须解码为 32 字节；AES-256-CBC、IV、32 字节 PKCS#7 和明文布局严格遵循企微协议，不自行替换算法。
- 解密后必须验证 receiveid 等于配置 CorpID，明文消息的 AgentID 必须等于配置应用 ID。
- HTTP 请求体、密文、解密明文和命令均有明确大小上限；XML 禁止 DTD、实体和异常重复关键字段。
- 默认只接受服务器时钟前后 300 秒内的请求；nonce 只校验格式并参与签名，不作为一次性值。已成功入队的超窗重试可以安全 ACK，未知超窗请求不得产生任务。
- 原始 POST body 最大 64 KiB、解密 XML 最大 32 KiB；两层 XML 使用 `defusedxml`，限制深度、元素数和关键字段唯一性，并拒绝压缩请求体。
- 回调只在完成验签、解密、授权、原子去重与任务入队后返回空 `200`；不在请求内搜索或下载。
- Redis 不可用时 fail closed：返回可重试错误且不执行任务，不静默降级为进程内幂等。
- 去重占位与 Stream 入队由一个 Redis Lua 操作完成；消息使用 MsgId，事件键包含 AgentID、FromUserName、CreateTime、MsgType/Event。
- 选择 token 使用密码学安全的高熵随机值，不把用户、候选或秘密编码进 token；Redis 中绑定 userid、request id、候选集版本和 TTL。
- 选择消费采用 `issued -> consumed(job_id)` 原子状态转换并与任务入队同一操作；正确重试返回同一 job，错误用户不消耗 token。
- 日志只记录关联 ID、阶段、结果码及不可逆的去重/用户摘要；不得记录 Token、AESKey、CorpSecret、原始密文、完整明文或完整 userid。
- FastAPI 使用 application factory、lifespan 和可覆盖依赖；Uvicorn/反向代理不得记录 callback query string。
- Phase 1 生产契约以 Redis 7.2+ 为最低版本，先支持 URL 指向的 standalone 服务；Sentinel/Cluster 在明确部署需求前不做隐式支持。生产应使用 ACL，跨主机连接应使用 TLS，幂等键不得处于可随意驱逐的缓存策略下。

## Scope

### Included

- `WeComSettings`：CorpID、AgentID、Token、EncodingAESKey、白名单、回调大小/时间/TTL 限制；
- 企微 SHA-1 签名和 AES-CBC 加解密窄适配层；
- 安全 XML 信封与文本/事件消息解析；
- 搜索文本、数字选择、翻页、取消和不支持命令的确定性解析；
- Redis 原子消息 claim+enqueue 与一次性选择上下文；
- FastAPI GET URL 验证与 POST 快速 ACK 路由；
- 可注入的时钟、状态存储和任务投递边界；
- 结构化、脱敏的错误与审计日志；
- 单元测试、协议夹具和 Redis 契约测试。

### Deferred

- 企业微信 access token 和主动发消息 API；
- 真实音乐源、并发搜索、候选排序和文件大小展示；
- 下载、分类和落盘；
- Telegram、AI、插件执行与管理后台；
- 生产 TLS/反向代理配置。

## Execution units

### Unit 1 — Configuration, dependencies and crypto/XML boundary

1. RED：配置秘密遮蔽、密钥长度、白名单、签名、官方协议字节布局、错误签名/padding/receiveid、XML 攻击与大小边界测试。
2. GREEN：加入固定版本 `cryptography` 和安全 XML 依赖，实现最小配置、crypto 与 parser。
3. 验证目标测试、全套测试、依赖许可证和秘密扫描后提交。

### Unit 2 — Commands and Redis state contracts

1. RED：命令边界、并发重复消息只入队一次、Redis 故障 fail-closed、选择 token 绑定/过期/重放测试。
2. GREEN：加入固定版本 `redis-py`，实现状态协议、内存测试替身及生产 Redis Lua 适配器。
3. 使用独立临时 Redis 实例验证 Lua 原子语义；不得把 Redis 加入项目 Compose。

### Unit 3 — Callback service and FastAPI integration

1. RED：GET challenge 原样响应、POST 验签顺序、白名单、AgentID、重复回调、快速空 ACK、错误映射与日志脱敏测试。
2. GREEN：组合 service 与 `/wecom/callback` GET/POST，使用 FastAPI 依赖注入隔离外部 Redis。
3. 复跑全套测试，执行本地 Uvicorn 探针，并在 Debian Docker 测试机验证镜像、外部临时 Redis、健康状态和回调夹具。

## Acceptance

- 每个生产行为都有已观察的 RED 和后续 GREEN；完整 `pytest -q -W error` 通过。
- GET challenge 正确验签、解密并以无引号/无 BOM/无额外换行的纯文本返回。
- POST 在安全校验、白名单与原子入队完成后快速返回空 `200`；重复消息只生成一个任务。
- 错误签名、错误 CorpID/AgentID、非白名单、恶意/超大 XML、过期请求和 Redis 故障不会执行任务。
- 选择上下文绑定用户、短期有效、不可枚举且只消费一次；错误用户不能消耗合法 token。
- 日志和异常不泄露配置秘密、密文、完整消息内容或完整 userid。
- Docker Compose 仍只有两个应用服务，插件容器仍无主服务秘密、Redis 配置或额外挂载。
- 真实企微应用的公网 TLS、字段与投递行为若未联调，Phase 1 状态必须标为“实现完成，真实应用门待验证”，不得宣称完全完成。

## Rollback

每个执行单元独立签名提交并推送。失败时使用新的 `git revert` 提交回滚对应单元；不使用 `git reset --hard`，不覆盖 Phase 0 历史。
