#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# One-shot bootstrap for the Virex portal control plane.
#
# Usage:
#   cp .env.example .env             # fill in secrets
#   ./bootstrap.sh                   # apply migrations + seed
#
# Or pass flags:
#   ./bootstrap.sh --fresh           # drop & recreate portal schema
#                                    # (keeps postgres up; does NOT drop DB)
#   ./bootstrap.sh --seed-only       # skip alembic, just run seed
#
# This script assumes Postgres is already reachable at $VIREX_DATABASE_URL
# (typically via `docker compose up -d postgres`).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."  # repo root (where alembic.ini lives)

if [[ ! -f deploy/vps/.env ]]; then
    echo "❌ deploy/vps/.env missing — copy from .env.example and fill secrets first:"
    echo "   cp deploy/vps/.env.example deploy/vps/.env"
    exit 2
fi
# shellcheck disable=SC1091
set -a; source deploy/vps/.env; set +a

FRESH=0
SEED_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --fresh) FRESH=1 ;;
        --seed-only) SEED_ONLY=1 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# 1. Install portal deps + alembic.
echo "==> sync portal deps"
uv sync --project portal --extra dev 2>&1 | tail -3

# 2. (Optional) Drop schema before applying migrations.
if (( FRESH )); then
    echo "==> dropping schema (--fresh)"
    cd "$SCRIPT_DIR/.."
    VIREX_DATABASE_URL="$VIREX_DATABASE_URL" \
        uv run --project portal python -c "
import asyncio
from sqlalchemy import text
from core.database import engine
async def drop():
    async with engine.begin() as conn:
        await conn.execute(text('DROP SCHEMA IF EXISTS public CASCADE'))
        await conn.execute(text('CREATE SCHEMA public'))
asyncio.run(drop())
print('schema dropped + recreated')
"
fi

# 3. Apply migrations.
if (( ! SEED_ONLY )); then
    echo "==> alembic upgrade head"
    cd "$SCRIPT_DIR/.."
    VIREX_DATABASE_URL="$VIREX_DATABASE_URL" \
        uv run --project portal alembic upgrade head
fi

# 4. Seed.
echo "==> seed"
cd "$SCRIPT_DIR/.."
VIREX_DATABASE_URL="$VIREX_DATABASE_URL" \
    uv run --project portal python -m portal.seed

echo ""
echo "✅ Portal ready on http://127.0.0.1:8000"
echo "   Login as ${VIREX_BOOTSTRAP_ADMIN_EMAIL:-admin@acme.example.com}"