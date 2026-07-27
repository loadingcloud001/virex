# SPDX-License-Identifier: Apache-2.0
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Docker CLI + docker compose plugin are required so edge-agent can
# run `docker compose up -d` on reconcile. We mount /var/run/docker.sock
# from the host — the binaries in this image talk to that daemon.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.4.0.tgz \
        | tar -xz -C /tmp && \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker && \
    rm -rf /tmp/docker /var/lib/apt/lists/*

# Docker compose plugin v2.x static binary (the `compose` PyPI package
# is unrelated — it's a function-composition library).
RUN mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -fsSL \
        https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64 \
        -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

RUN pip install --quiet \
        "httpx>=0.27" \
        "pydantic>=2.5" \
        "pydantic-settings>=2" \
        "structlog>=24" \
        "tenacity>=8" \
        "nvidia-ml-py>=7" \
        "psutil>=5.9" \
        "jinja2>=3.1" \
        "pyyaml>=6" \
        "python-dotenv>=1" \
        # Hot-reload: inotify on workers.yaml.
        "watchdog>=6" \
        # Used by reconcile.restart_mediamtx() to call Docker SDK.
        "docker>=7"

WORKDIR /app
COPY src/        ./src/
COPY templates/  ./templates/

CMD ["python", "-m", "src.main"]