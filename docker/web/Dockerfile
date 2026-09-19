# The administration panel, built once into a standalone Next.js server.
#
# The panel has exactly one backend and it is this repository's own app: the
# browser only ever calls `/api/*` on the panel, and the build rewrites that hop
# to the app's `/admin/*` routes. Next resolves rewrites while it builds, so
# `MUSICDL_API_ORIGIN` is a build argument here -- set to the Compose service
# name, because that is how one container reaches another.
FROM node:22.23.2-slim AS deps

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22.23.2-slim AS builder

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /build
ARG MUSICDL_API_ORIGIN=http://musicdl:8000
ENV MUSICDL_API_ORIGIN=${MUSICDL_API_ORIGIN}
COPY --from=deps /build/node_modules ./node_modules
COPY web/package.json web/package-lock.json ./
COPY web/next.config.mjs web/postcss.config.mjs web/tsconfig.json web/next-env.d.ts ./
COPY web/src ./src
RUN npm run build

FROM node:22.23.2-slim AS runner

ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
RUN groupadd --gid 10001 adminpanel \
    && useradd --uid 10001 --gid adminpanel --create-home --shell /usr/sbin/nologin adminpanel
# `standalone` carries the server and the slice of node_modules it needs; the
# static chunks and the (absent) `public/` directory are copied beside it,
# because the standalone layout does not include them.
COPY --from=builder --chown=10001:10001 /build/.next/standalone ./
COPY --from=builder --chown=10001:10001 /build/.next/static ./.next/static
# The runtime keeps its on-disk cache here. Compose mounts a tmpfs over it so
# the root filesystem can stay read-only.
RUN install -d -o 10001 -g 10001 /app/.next/cache
USER 10001:10001
CMD ["node", "server.js"]
