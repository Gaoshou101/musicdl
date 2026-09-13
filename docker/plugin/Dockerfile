FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS deno-download

ARG TARGETARCH
ARG DENO_VERSION=2.9.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/deno
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) deno_file="deno-x86_64-unknown-linux-gnu.zip"; deno_sha="394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2" ;; \
        arm64) deno_file="deno-aarch64-unknown-linux-gnu.zip"; deno_sha="9a46afc6c392c7cd2ff71a31558935545b46408d0e87f7a86908c712721c046e" ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl --fail --location --proto '=https' --tlsv1.2 --output "${deno_file}" \
      "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/${deno_file}"; \
    printf '%s  %s\n' "${deno_sha}" "${deno_file}" | sha256sum -c -; \
    unzip -q "${deno_file}" -d /out; \
    chmod 0755 /out/deno; \
    rm -f "${deno_file}"

FROM python:3.12.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libseccomp2=2.5.4-1+deb12u1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno-download /out/deno /usr/bin/deno
RUN deno --version

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --timeout 120 --root-user-action=ignore .
RUN groupadd --gid 10001 musicdl \
    && useradd --uid 10001 --gid musicdl --create-home --shell /usr/sbin/nologin musicdl
USER 10001:10001
