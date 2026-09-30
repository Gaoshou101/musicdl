# 项目优化复核记录（2026-09-30）

本轮继续复核 `codex/comprehensive-optimization`，基于已提交的 `874e6aa` 补齐资源生命周期、管理状态、企业微信交互和验证工具的具体问题。按用户指示，由当前模型独立实施和检查，没有启用 Sol/Luna，也没有进行独立模型验收。

## 审查范围

对 `src`、`web/src` 的 94 个文件进行清单和风险模式搜索，重点检查异步任务、线程、网络与文件操作、异常处理及前端轮询。人工阅读集中于应用启动/重载/关闭、管理员 API、Telegram、平台搜索、Redis worker、插件 client/broker/supervisor、媒体下载/传输/探测、AI client、前端搜索与日志以及 CI/发布闸门。

这不是所有源文件逐行审查的证明。没有测量生产吞吐或延迟，也不宣称性能提升百分比；本轮性能改动主要确保并发、内存与资源释放受到控制。

## 已修复的问题

| 问题与结果 | 代码证据 | 验证证据 |
| --- | --- | --- |
| 启动异常或取消后日志 handler 残留；将初始构建纳入生命周期的 finally 清理。 | `src/musicdl/app.py`: `create_app.lifespan` | `test_startup_failure_always_uninstalls_logging` |
| 重载关闭仍被请求使用的旧运行实例；请求借用实例，清理等待借用结束，旧实例清理任务由应用持有，不随重载请求取消。关闭与重载串行，关闭开始后拒绝新的重载。 | `src/musicdl/app.py`: `_Runtime.borrow/aclose`, `reload_runtime`; `src/musicdl/admin/portal.py`: `uses_runtime`; `_admin_probes` 对实际访问的实例进行借用；WeCom POST 借用包含在原四秒期限内。 | `test_runtime_reload_preserves_inflight_search_and_closes_after_release`, `test_cancelled_request_releases_runtime_and_close_is_idempotent`, `test_health_probe_keeps_its_dependency_alive_during_reload`, `test_shutdown_waits_for_reload_assembly_and_closes_the_replacement` |
| 管理器多字段更新在后续字段验证失败时留下部分修改，注册持久化失败时留下幽灵条目；先验证副本，持久化失败恢复内存状态。 | `src/musicdl/admin/management.py`: `SourceManager.register/update`，`BotManager` 复用该实现。 | 参数化管理器回归测试。 |
| 已撤销会话的 CSRF 引用未清理，登录限流键可无限增长；同步清理 CSRF，限流 map 最多保留 4096 个键，按最近接受时间清理过期键，容量满时不驱逐仍有效的限制。 | `src/musicdl/admin/auth.py`: `AdminAuth.revoke_session`, `RateLimiter.allow` | 会话撤销和限流容量/过期回归测试。 |
| Telegram 并发初始化可重复构建 client，logout 可在连接初始化时删除 session；每个 profile 串行初始化/连接/logout，关闭等待初始化并尝试清理所有 client。待验证登录的内存降级按 profile 隔离，新值优先于旧磁盘值。 | `src/musicdl/telegram/connector.py`: `_client`, `logout`, `disconnect`, `pending_login` | 并发 factory/connect、初始化期间关闭/logout、单 client 关闭失败及多 profile 登录回归测试。 |
| 搜索超时释放并发槽后，同步 DNS/网络仍在线程中运行，可反复堆积；broker 任务保留独立并发槽直至实际完成，异步调用仍遵守期限。 | `src/musicdl/sources/platform_search.py`: `_fetch_observation` | `test_catalogue_timeout_does_not_free_a_still_running_broker_slot` |
| 企业微信刷新通知占用字节预算，或重载改变每页数量，导致后续翻页重新分页而漏/重复显示候选；持久化 selection 的通知和每页数量，并在导航时复用。 | `src/musicdl/worker/selection.py`: `bind_user_selection`; `workers.py`: `_search_token`, `_handle_interaction`, `_rebind` | `test_navigation_reuses_frozen_notice_and_page_size` 和现有 worker 回归。 |
| 两个 Redis consumer group 共享重试键，某组 ack 可清除另一组预算；键使用 JSON 编码的 stream/group/message-id 三元组，死信携带 group。 | `src/musicdl/worker/stream.py`: `_retry_key`, `_ack_then_clear`, `_record_failure` | `test_consumer_groups_do_not_clear_each_others_retry_budget` 和现有 worker 回归。 |
| 服务重启后日志数字 ID 复用，前端把新日志当成重复记录；后端提供 generation/reset，前端传回 generation 并在重置时替换窗口，包括空窗口。 | `src/musicdl/admin/logs.py`: `LogBuffer.page`; `portal.py`: `service_logs`; `web/src/lib/api.ts`, `web/src/app/dashboard/logs/page.tsx` | 后端日志回归、`web/scripts/check-service-logs.mjs`；CI 加入该脚本。 |
| 发布闸门选择损坏的仓库虚拟环境，且 dry-run 隐藏实际 FAIL；子测试使用调用脚本的解释器，仅明确的 NOT_RUN 允许 dry-run 跳过。 | `scripts/release/run_gates.py`: `_python`, `report`, `main` | `test_child_suites_use_the_invoking_interpreter`, `test_dry_run_never_hides_a_completed_gate_failure`；闸门自检与 dry-run。 |

新增集中回归位于 `tests/unit/test_audit_regressions.py`，并更新已有日志、闸门与 worker 测试。

## 验证结果

- 最终完整 Python 套件：1872 passed、113 skipped，退出码 0，耗时 110.29 秒；完成日志、JUnit XML 和退出码保存于仓库外 `musicdl-audit-final-ed23741b0ee041759ed6e36f73f5a23e` 临时目录。
- 最后补齐生命周期后的定向回归：49 passed，退出码 0。
- 发布闸门 `--self-test --dry-run`：退出码 0。篡改样例输出的 FAIL 是自检期望的拒绝；正式 compose/proxy/Telegram 隔离/media/admin 检查均 PASS，内部 admin 套件 246 passed。
- 前端 `npx tsc --noEmit`、`npm run check:service-logs` 成功。前序收尾已经完成 `check:search-probe`、`check:source-import`（14 项）与生产静态构建，退出码均为 0；之后没有改变前端生产逻辑，仅补齐后端生命周期。
- `git diff --check` 成功。

仓库 `.venv` 存在缺失的 Starlette 文件及损坏的 pip 元数据。本轮使用独立临时 Python 环境安装项目依赖完成验证，没有修改项目依赖声明。pytest 临时目录、日志和 JUnit XML 保存在仓库外；保留仓库原有未跟踪文件。

## 环境限制与后续候选

本机缺少 Docker Engine、Deno 和测试 Redis，另有 POSIX 专用测试。相关测试明确跳过；真实微信 HTTPS 回调及 Redis 恢复演练未执行。dry-run 成功不能作为这些集成验证已通过的证据。

以下是后续优化候选，需要明确资源所有权或协议并补充对应验证，本轮没有宣称完成：

- `admin/portal.py` 的登录/修改凭证仍同步调用 PBKDF2。移到线程前需限制 CPU 工作准入，并确保校验与凭证变更之间的版本一致。
- `media/download.py` 的文件写入、fsync、`_hash_file` 及时长解析、`admin/store.py:AdminStateStore.save` 仍有同步文件工作。线程化需维持发布 fencing、取消清理和持久化快照顺序。
- `admin/auth.py:AdminAuth` 的会话仍没有服务端 TTL/容量限制；需要定义有效期和过期后的浏览器行为。
- `ai/client.py:OpenAICompatibleClient.complete_json` 每次新建 HTTP client；复用前需定义设置变化和关闭责任。
- 发布静态契约仍使用文本匹配；CI 的分支 push 和 PR 都触发运行；前端 `lint` 仍为 `next lint`，未配置独立 ESLint 检查。这些需要单独验证工具配置。
- 数字选择仍在 worker 处理时绑定当前 selection，而非回调到达时绑定；跨 consumer group 的延迟响应需要协议级验证。
- 已运行的同步 DNS/网络不能被 asyncio 超时中断，会自然结束。broker 槽限制作用于单个运行实例，不是跨重载的进程级总上限。

本轮没有更改版本、发布镜像或 GitHub Release，也没有部署或运行生产环境恢复操作。
