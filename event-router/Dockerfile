# SPDX-License-Identifier: Apache-2.0
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --quiet \
        "paho-mqtt>=2" \
        "redis>=5" \
        "sqlalchemy>=2" \
        "asyncpg>=0.29" \
        "pydantic>=2.5" \
        "structlog>=24" \
        "httpx>=0.27" \
        "tenacity>=8" \
        "python-dotenv>=1"

COPY src/ ./src/

CMD ["python", "-m", "src.main"]