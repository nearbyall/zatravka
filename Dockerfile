FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Chromium serves the Threads web mode. Inside the container the container itself
# is the sandbox, hence no Chromium sandbox.
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV ZATRAVKA_CHROMIUM=/usr/bin/chromium ZATRAVKA_CHROMIUM_NO_SANDBOX=1

COPY pyproject.toml README.md ./
COPY zatravka ./zatravka
RUN pip install . && mkdir -p /app/data

# compose.yaml sets the real user (ZATRAVKA_UID/GID) so files in ./data belong to you,
# and mounts config.yaml, .env and the data/ volume.
ENV HOME=/tmp
USER 1000
CMD ["zatravka", "daemon"]
