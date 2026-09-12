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
| HTTPX | 0.28.1 | Test; later outbound HTTP | BSD-3-Clause | https://pypi.org/project/httpx/ |
| PyYAML | 6.0.3 | Test-only Compose parsing | MIT | https://pypi.org/project/PyYAML/ |
| pytest | 9.1.1 | Test | MIT | https://pypi.org/project/pytest/ |

Versions are exact at the direct-dependency level. A transitive, hash-pinned lock must be generated and tested before the first release image is accepted.

Telethon and `openai-python` are intentionally absent from Phase 0. They will be evaluated and introduced with their own protocol, credential and fallback tests in Phase 3 and Phase 5 respectively. Redis is introduced here for the async state adapter; real Redis integration remains environment-gated.

The reference repository `liqman/tgmusic-wecom` exposes deployment/configuration files but no reviewable business source or LICENSE in the researched snapshot. Its implementation is therefore not copied into musicdl.
