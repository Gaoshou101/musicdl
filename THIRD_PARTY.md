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
| Deno | 2.9.6 | Restricted JavaScript plugin runtime | MIT | https://github.com/denoland/deno/releases/tag/v2.9.6 |
| libseccomp2 | 2.5.4-1+deb12u1 | Restricted Python syscall filter | LGPL-2.1-or-later | https://packages.debian.org/bookworm/libseccomp2 |

Versions are exact at the direct-dependency level. A transitive, hash-pinned lock must be generated and tested before the first release image is accepted.

Telethon was introduced in Phase 3 behind injectable connector and response-decoder boundaries. Real-account login, the selected public Bot protocol, proxy conditions, and service limits remain deployment-environment validation gates. `openai-python` remains deferred to Phase 5. Redis integration remains environment-gated.

The reference repository `liqman/tgmusic-wecom` exposes deployment/configuration files but no reviewable business source or LICENSE in the researched snapshot. Its implementation is therefore not copied into musicdl.

The twelve lx custom-source scripts supplied on 2026-09-16 as adaptation blueprints are not vendored either, and that is a product decision rather than an oversight. Only `HYWmusic_beta` and `K x H测试` declare a license in their header (MIT); the other ten declare none, and this file already refuses to copy third-party code that has no reviewable LICENSE. None of them is baked into the image or seeded into the data volume, so no third-party source ships with a deployment. A script reaches an installation only when an operator imports it through the administration portal (`POST /admin/sources`), which is also the only route that can grant the per-source egress widening such a script needs: the analyzer verdict, including every grant it would have to be given, is what the operator reviews before any byte is stored.

Phase 6 runtime supply-chain evidence:

- Deno is downloaded only from `https://github.com/denoland/deno/releases/download/v2.9.6/`.
  The amd64 `deno-x86_64-unknown-linux-gnu.zip` SHA-256 is
  `394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2`; the arm64
  `deno-aarch64-unknown-linux-gnu.zip` SHA-256 is
  `9a46afc6c392c7cd2ff71a31558935545b46408d0e87f7a86908c712721c046e`.
- libseccomp2 is installed from Debian Bookworm at exactly `2.5.4-1+deb12u1`;
  source package and license details are available from the Debian package page above.
- Neither Deno nor libseccomp is exposed to plugins as an installation surface:
  the image contains the preinstalled Deno binary and Python loads libseccomp
  through the trusted host, while plugins have no package manager, subprocess,
  filesystem, or network capability.
