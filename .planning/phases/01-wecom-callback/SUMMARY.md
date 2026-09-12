# Phase 1 Summary — WeCom Callback Vertical Slice

## Status

实现完成；真实企业微信应用与公网 TLS 联调门待验证。Phase 1 尚不标记为完全完成。

## Delivered

- 企业微信加密回调协议边界：SHA-1 签名、AES-256-CBC、32 字节 PKCS#7、CorpID 校验和防御性 XML 解析。
- 成员白名单与 AgentID 授权；默认前后 300 秒时间窗口。
- 文本搜索、数字选择、翻页、取消和不支持命令的确定性解析。
- Redis Lua 原子消息去重与 Stream 入队；高熵、绑定用户和请求的一次性选择上下文。
- FastAPI application factory/lifespan；GET URL challenge、POST 空 ACK、无依赖 `/healthz` 和 Redis `/readyz`。
- 64 KiB 流式请求体限制、32 KiB 解密明文限制、压缩体拒绝和 Redis 故障 503 fail-closed。
- Redis 连接/操作超时默认 2 秒且可通过环境变量配置。
- Uvicorn access log 禁用；审计日志仅记录固定长度 request fingerprint，不记录原始查询串、用户、消息、密文或配置秘密。

## Commits

- `5bc9ab3` — secure WeCom protocol boundary
- `dcefe37` — atomic WeCom state contracts
- `82a5e87` — secure WeCom callback gateway

以上提交均使用 SSH 签名并已推送至 `origin/codex/phase-1-wecom-callback`。

## Verification evidence

### Local

- Unit 3 目标测试：44 passed。
- Phase 1 基线完整测试：96 passed, 1 skipped；唯一跳过项为需要真实 Redis URL 的集成测试。
- `pip check`：无损坏依赖。
- `git diff --check`：通过。
- 独立 `luna_tester` 复核：无 blocker/major；其唯一日志注入 minor 已在提交前修复。

### Redis 7.2.16 integration

- 独立 Redis 并发集成测试：1 passed。
- 启用真实 Redis 后的当时完整套件：73 passed。
- 20 个并发重复消息只产生一个 Stream entry；并发重复选择只产生同一 job，错误用户不会消耗 token。

### Debian Docker runtime

- 临时工作树制品 SHA-256 在本地与远端一致。
- 生产镜像构建成功；主服务以只读根文件系统、全 capability drop 和 `no-new-privileges` 启动。
- Redis 7.2.16 下 `/healthz` 和 `/readyz` 返回 200。
- 加密 GET challenge 返回精确明文，无额外换行。
- 加密 POST 返回空 200，Redis Stream 长度由 0 变为 1；同 MsgId 重试仍为空 200 且 Stream 保持 1。
- 停止 Redis 后 `/readyz` 与新 POST 均返回 503，实测约 0.01 秒。
- 容器日志未出现 callback 查询串。
- 测试容器、网络、镜像标签、远端制品和本地临时制品均已清理。

## Remaining acceptance gate

需要用户提供或自行配置真实企业微信自建应用的 CorpID、AgentID、Token、EncodingAESKey、白名单成员和公网 HTTPS 回调地址，完成：

1. 企业微信后台 URL 验证；
2. 真实文本消息字段与重试行为；
3. 反向代理和公网 TLS；
4. 代理层 access log 不保留 callback query string 的证明。

在这些检查完成前，路线图保持“实现完成，真实应用门待验证”。

## Rollback

若需回滚，按逆序创建 `git revert 82a5e87`、`git revert dcefe37`、`git revert 5bc9ab3` 的新提交；不要重写已推送历史或 force-push。
