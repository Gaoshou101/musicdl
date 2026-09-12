# Third-party dependency baseline

Snapshot date: 2026-09-11.

| Dependency | Version | Scope | License | Primary evidence |
|---|---:|---|---|---|
| Python | 3.12.x | Runtime | PSF | https://www.python.org/downloads/ |
| FastAPI | 0.141.1 | Runtime | MIT | https://pypi.org/project/fastapi/ |
| pydantic-settings | 2.15.0 | Runtime | MIT | https://pypi.org/project/pydantic-settings/ |
| Uvicorn | 0.52.4 | Runtime | BSD-3-Clause | https://pypi.org/project/uvicorn/ |
| HTTPX | 0.28.1 | Test; later outbound HTTP | BSD-3-Clause | https://pypi.org/project/httpx/ |
| PyYAML | 6.0.3 | Test-only Compose parsing | MIT | https://pypi.org/project/PyYAML/ |
| pytest | 9.1.1 | Test | MIT | https://pypi.org/project/pytest/ |

Versions are exact at the direct-dependency level. A transitive, hash-pinned lock must be generated and tested before the first release image is accepted.

Telethon and `openai-python` are intentionally absent from Phase 0. They will be evaluated and introduced with their own protocol, credential and fallback tests in Phase 3 and Phase 5 respectively. Redis client code is also deferred until a phase performs real Redis I/O; Phase 0 defines only the external Redis configuration contract.

The reference repository `liqman/tgmusic-wecom` exposes deployment/configuration files but no reviewable business source or LICENSE in the researched snapshot. Its implementation is therefore not copied into musicdl.
