FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Chromium — для веб-режима Threads (публичные профили без входа). Песочницу Chromium
# внутри контейнера заменяет изоляция самого контейнера.
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV IDEAMINER_CHROMIUM=/usr/bin/chromium IDEAMINER_CHROMIUM_NO_SANDBOX=1

COPY pyproject.toml README.md ./
COPY ideaminer ./ideaminer
RUN pip install . && mkdir -p /app/data

# Пользователь задаётся в compose.yaml (IDEAMINER_UID/GID из .env), чтобы файлы в ./data были ваши
ENV HOME=/tmp
USER 1000
# config.yaml и .env монтируются снаружи (см. compose.yaml), data/ — том с базой
CMD ["ideaminer", "daemon"]
