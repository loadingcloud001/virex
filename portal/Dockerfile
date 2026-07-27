# SPDX-License-Identifier: Apache-2.0
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --quiet \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.29" \
        "sqlalchemy>=2" \
        "asyncpg>=0.29" \
        "psycopg[binary]>=3.1" \
        "pydantic[email]>=2.5" \
        "pydantic-settings>=2" \
        "structlog>=24" \
        "httpx>=0.27" \
        "alembic>=1.13" \
        "python-dotenv>=1" \
        "bcrypt>=4.0" \
        "pyjwt>=2" \
        "jinja2>=3.1" \
        "python-multipart>=0.0.9"

COPY core/        ./core/
COPY middleware/  ./middleware/
COPY models/      ./models/
COPY schemas/     ./schemas/
COPY services/    ./services/
COPY api/         ./api/
COPY templates/   ./templates/
COPY migrations/  ./migrations/
COPY seed.py      ./seed.py
COPY app.py       ./app.py
# alembic.ini lives at the portal/ subdirectory in the build context.
# (The portal builds from /portal, not from the repo root.)
COPY alembic.ini  ./alembic.ini

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]