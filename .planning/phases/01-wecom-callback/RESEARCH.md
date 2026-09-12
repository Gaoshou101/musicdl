# Phase 1 Research — WeCom Callback

## Evidence boundary

Research date: 2026-09-12. The official developer page at `https://developer.work.weixin.qq.com/document/path/90238` was not directly readable by the current browser tool. Protocol details were cross-checked against the enterprise WeChat documentation mirror that points to that official path and Tencent-team sample source. A real self-built application probe remains mandatory before Phase 1 can be called fully validated.

## Protocol findings

- URL verification uses `msg_signature`, `timestamp`, `nonce` and URL-decoded `echostr`; the server verifies the SHA-1 signature, decrypts `echostr`, and returns the plaintext without quotes, BOM or an extra newline.
- Encrypted POST callbacks carry an outer XML `Encrypt` field. The signature is `SHA1(sort(token, timestamp, nonce, encrypt))` and must be checked before AES decryption.
- `EncodingAESKey + "="` decodes to 32 bytes. The protocol uses AES-256-CBC with the first 16 key bytes as IV, 32-byte PKCS#7 padding, and plaintext layout `random16 + uint32_be(message_length) + message + receiveid`.
- For a self-built application, receiveid is CorpID and must be checked after decrypting.
- Text messages identify the member with `FromUserName`, include `CreateTime`, `MsgType`, `Content`, `MsgId` and `AgentID`.
- The platform retries callbacks when it does not receive a successful response in time. Messages should be deduplicated by MsgId; events without MsgId use identity and creation-time fields.
- Slow search/download work must not run in the callback request. The safe path is validation, authorization and durable atomic enqueue followed by an empty HTTP 200.

## Reuse findings

`liqman/tgmusic-wecom` currently exposes README, `config.ini` and `docker-compose.yml`, but no reviewable business source or LICENSE on its main branch. Its callback path and text/number command interaction are product references only. No implementation code is copied.

Tencent-team `sbzhu/weworkapi_python` documents the protocol and provides interoperable examples, but describes itself as sample code that may contain bugs or lag the official API. Its historical Python code uses PyCrypto and legacy compatibility constructs, and the repository has no confirmed standard LICENSE. It is evidence, not a production dependency.

## Dependency decisions

- `cryptography`: maintained implementation of AES/CBC primitives; its hazardous-material API is isolated behind a minimal WeCom-specific adapter and guarded by signature verification and strict parsing.
- `defusedxml`: defensive XML parser for untrusted callback payloads.
- `redis-py`: official Python Redis client with asyncio support; Phase 1 requires Redis 7.2+ and uses static Lua scripts for atomic stream/state transitions.

## Primary and direct evidence

- Official target page: https://developer.work.weixin.qq.com/document/path/90238
- Receive-message mirror: https://wdk-docs.github.io/wework-docs/server/basic/message-push/receive-messages-and-events/
- Encryption scheme mirror: https://wdk-docs.github.io/wework-docs/appendix/encryption-and-decryption/
- Tencent-team repository: https://github.com/sbzhu/weworkapi_python
- Python 3 callback example: https://github.com/sbzhu/weworkapi_python/blob/master/callback_python3/WXBizMsgCrypt.py
- `cryptography` primitives: https://cryptography.io/en/stable/hazmat/primitives/
- Python XML security guidance: https://docs.python.org/3.12/library/xml.html
- Redis Lua atomic execution: https://redis.io/docs/latest/develop/programmability/eval-intro/
- Redis SET semantics: https://redis.io/docs/latest/commands/set/

## Real-service gates

- Public HTTPS callback registration and URL challenge in the user's actual enterprise application;
- observed CorpID, AgentID, FromUserName, MsgId and event fields;
- real retry timing and duplicate delivery;
- active-message API permissions and access-token lifecycle;
- external Redis version/topology, ACL/TLS, persistence and eviction policy;
- reverse-proxy and Uvicorn access-log proof that callback query strings are not retained.
