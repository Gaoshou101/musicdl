FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --root-user-action=ignore .
RUN groupadd --gid 10001 musicdl \
    && useradd --uid 10001 --gid musicdl --create-home --shell /usr/sbin/nologin musicdl \
    && install -d -o 10001 -g 10001 /data/music /data/app /data/telegram-sessions
USER 10001:10001
