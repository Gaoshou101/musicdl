# Third-party dependency baseline

Snapshot date: 2026-09-12.

| Dependency | Version | Scope | License | Primary evidence |
|---|---:|---|---|---|
| Python | 3.12.x | Runtime | PSF | https://www.python.org/downloads/ |
| FastAPI | 0.141.1 | Runtime | MIT | https://pypi.org/project/fastapi/ |
| pydantic-settings | 2.15.0 | Runtime | MIT | https://pypi.org/project/pydantic-settings/ |
| Uvicorn | 0.52.4 | Runtime | BSD-3-Clause | https://pypi.org/project/uvicorn/ |
| cryptography | 50.0.1 | Runtime; WeCom AES | Apache-2.0 OR BSD-3-Clause | https://pypi.org/project/cryptography/ |
| defusedxml | 0.7.1 | Runtime; XML parsing | PSF-2.0 | https://pypi.org/project/defusedxml/ |
| redis | 8.1.0 | Runtime; async Redis state adapter | MIT | https://pypi.org/project/redis/ |
| Telethon | 1.45.0 | Runtime; Telegram user-account sessions and Bot conversations | MIT | https://pypi.org/project/Telethon/ |
| HTTPX | 0.28.1 | Test; later outbound HTTP | BSD-3-Clause | https://pypi.org/project/httpx/ |
| PyYAML | 6.0.3 | Test-only Compose parsing | MIT | https://pypi.org/project/PyYAML/ |
| pytest | 9.1.1 | Test | MIT | https://pypi.org/project/pytest/ |

Versions are exact at the direct-dependency level. A transitive, hash-pinned lock must be generated and tested before the first release image is accepted.

Telethon was introduced in Phase 3 behind injectable connector and response-decoder boundaries. Real-account login, the selected public Bot protocol, proxy conditions, and service limits remain deployment-environment validation gates. `openai-python` remains deferred to Phase 5. Redis integration remains environment-gated.

The reference repository `liqman/tgmusic-wecom` exposes deployment/configuration files but no reviewable business source or LICENSE in the researched snapshot. Its implementation is therefore not copied into musicdl.
