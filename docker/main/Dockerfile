# The console, built from the same commit as the service that serves it.
#
# It is a client-only Next application: `next build` emits static files, nothing
# in production needs Node, and the runtime stage below only has to carry the
# export. Building it here rather than into an image of its own is what keeps a
# deployment to the app and the plugin runner instead of three containers.
FROM node:22.23.2-slim AS panel

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/next.config.mjs web/postcss.config.mjs web/tsconfig.json web/next-env.d.ts ./
COPY web/src ./src
RUN npm run build


FROM python:3.12.14-slim AS app-package

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/musicdl ./src/musicdl
COPY src/musicdl_plugin_runner ./src/musicdl_plugin_runner
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=app-package /wheels/musicdl-1.0.3-py3-none-any.whl /tmp/musicdl-1.0.3-py3-none-any.whl
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/musicdl-1.0.3-py3-none-any.whl \
    && rm -f /tmp/musicdl-1.0.3-py3-none-any.whl
# The console travels in the image and the app is told where it landed. Nothing
# else sets this: a checkout that never ran the panel build has no console, and
# saying so is how it answers no front page instead of an empty one.
COPY --from=panel --chown=10001:10001 /build/out /app/panel
ENV MUSICDL_ADMIN__PANEL_ROOT=/app/panel
RUN groupadd --gid 10001 musicdl \
    && useradd --uid 10001 --gid musicdl --create-home --shell /usr/sbin/nologin musicdl \
    && install -d -o 10001 -g 10001 /data/music /data/app /data/telegram-sessions
USER 10001:10001
