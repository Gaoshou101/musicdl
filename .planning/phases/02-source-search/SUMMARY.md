# Phase 2 Summary — Source Abstraction and Deterministic Search

## Status

本阶段完成。真实 Telegram Bot 和外部插件来源仍按路线图留给 Phase 3 与 Phase 6，不属于本阶段完成声明。

## Delivered

- 有界、冻结且拒绝额外字段的统一 `Candidate` 模型，包含来源版本、标题、艺术家、专辑、时长、码率、格式与文件大小。
- Unicode NFKC 与空白规范化；可选空字段统一为 `None`。
- 严格来源注册表：来源 ID 唯一、启停值必须为布尔值、优先级必须为整数，默认最多 64 个来源。
- 多来源 asyncio 并发查询、独立超时、异常隔离、来源/版本绑定校验，以及默认每来源最多 100 条结果。
- 拒绝字符串、generator、非 Sequence 和超限来源结果，状态字段不包含查询、原始响应或异常文本。
- 跨来源版本去重；确定性 fallback 按相关性、已知无损格式、码率、元数据完整度、大小、来源优先级和稳定标识排序。
- 候选集版本使用完整公开候选表示的稳定 SHA-256；来源完成顺序不会改变候选、状态或版本。
- 企微候选文本包含序号、标题、艺术家、来源及版本、格式和 IEC 文件大小；总长度硬限制为 2048 UTF-8 字节。
- 长中文字段按 code point 安全缩短且必需字段保持非空；专辑、时长和码率只在预算允许时追加。

## Commits

- `fbb3053` — deterministic multi-source search foundation
- `4d8f634` — strict source validation, bounded results and quality ordering
- `12636dd` — non-empty UTF-8-bounded result formatting and ranking regression coverage

以上提交均有 SSH 签名并已推送到 `origin/codex/phase-2-source-search`。

## Verification evidence

- 最终 Phase 2 聚焦测试：27 passed，启用 `-W error`。
- 独立 `luna_tester` 复核：Phase 2 verdict `ACCEPT`，无 blocker。
- Phase 2 工作树在并行 Telegram 文件出现前的完整测试：99 passed, 1 skipped；唯一跳过为需要真实 Redis URL 的 Phase 1 集成测试。
- `pip check`：无损坏依赖。
- `git diff --check`：通过。
- 已直接复现并修复两个审查反例：低码率结果因标题靠前而错误优先，以及长字段生成超过企微文本预算的消息。

## Boundaries

- 排序是基于已声明元数据的确定性规则，不声称检测真实听感或文件完整性；AI 重排在 Phase 5。
- 本阶段使用受控来源适配器，不声称已连接真实 Telegram Bot 或上传插件。
- 企微文本按当前项目契约限制为最多 2048 UTF-8 字节；后续主动消息发送器必须继续执行同一上限。
- Phase 1 callback 集成与 Phase 2 搜索执行器的接线尚未合入同一集成分支；共享工作区存在独立 Phase 3 改动，需在其稳定后串行整合。

## Rollback

若需回滚，在该分支按逆序为 `12636dd`、`4d8f634`、`fbb3053` 创建新的 `git revert` 提交；不要重写已推送历史或 force-push。
