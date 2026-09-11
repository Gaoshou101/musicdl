FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN groupadd --system --gid 10001 musicdl \
    && useradd --system --uid 10001 --gid musicdl --create-home musicdl
USER 10001:10001
