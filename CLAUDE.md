# CLAUDE.md - Project Coding Standards for AI Assistants

> ⚠️ **STATUS (2026-07-27)**: The body of this file was written for the
> original Phase 1 MVP design (Frigate + go2rtc + PostgreSQL + n8n) and
> is **partially stale**. The current v1 pilot is AGPL-3.0 and ships four
> Python modules: `ai-backend/`, `edge-agent/`, `event-router/`, `portal/`.
>
> The **Coding Standards** section (structlog, ruff E/F/I/W/UP/B/SIM,
> asyncio I/O, etc.) is still authoritative — the team's lint/test pipeline
> enforces it. The **Architecture Summary** and **Tech Stack** sections
> below describe the **original** Phase 1 stack, not the current one.
> Refer to [`README.md`](README.md) for the current architecture and
> [`docs/`](docs/) for module-level details.
>
> A full CLAUDE.md rewrite is on the roadmap; for now, do not rely on
> this file for module layout or technology choices — only for coding
> conventions.

> **This file is read by Claude Code and other AI coding assistants at the start of every session.**
> All generated code MUST follow these standards.

## Project Overview

**Product Name**: Virex
**Company**: Loading Cloud
**Type**: B2B Multi-Tenant Cloud VMS (Video Management System)
**Goal**: Provide centralized AI-powered CCTV monitoring as a service
**Stage**: Phase 1 - MVP (0-50 cameras, single tenant pilot)
**Repository**: github.com/loading/virex
**License**: MIT

## Architecture Summary

```
VPS (Hetzner)              RTX 4070 PC (Customer Edge)
┌──────────────────┐       ┌────────────────────────┐
│ Control Plane    │ ◄────►│ AI Edge                │
├──────────────────┤ Tailscale
│ - FastAPI Portal │       │ - Frigate (NVR+AI)     │
│ - PostgreSQL     │       │ - go2rtc (RTSP)        │
│ - Redis          │       │ - Edge Agent           │
│ - MinIO          │       │ - YOLOv8 Detection     │
│ - Mosquitto MQTT │       │ - OCR Worker           │
│ - Event Router   │       │ - Recording Storage    │
│ - n8n (notify)   │       └────────────────────────┘
│ - Grafana        │
└──────────────────┘
```

**Detailed design**: See `ARCHITECTURE.md`
**Phase plan**: See `ROADMAP.md`

---

## Tech Stack (MANDATORY)

### Backend
- **Python 3.11+** (use modern syntax: `match`, `|` type hints, `dict[str, int]`)
- **FastAPI 0.110+** with async/await everywhere
- **SQLAlchemy 2.0** async style (NOT 1.x)
- **Pydantic v2** (NOT v1)
- **Alembic** for DB migrations
- **asyncpg** as PostgreSQL driver

### Storage
- **PostgreSQL 16** (tenants, cameras, rules, events metadata)
- **Redis 7** (session cache, alert cooldown)
- **MinIO** (S3-compatible, video recordings + snapshots)

### Messaging
- **Mosquitto MQTT** (Frigate event bus)
- **paho-mqtt** Python client

### AI Edge (RTX 4070 PC)
- **Frigate 0.13+** (NVR + AI engine, Docker)
- **go2rtc** (RTSP relay, Frigate built-in)
- **YOLOv8** (Ultralytics, Frigate built-in)
- **llama.cpp + dots.ocr** (OCR worker)

### Notification
- **n8n** (workflow routing)
- **Telegram Bot API**
- **SMTP** (email)
- **Generic Webhook**

### Monitoring
- **Grafana** + **Prometheus**

### Deployment
- **Docker Compose** (NOT Kubernetes in Phase 1)
- **Tailscale WireGuard** (VPS ↔ PC encrypted tunnel)

---

## Multi-Tenant Rules (CRITICAL)

These rules are NON-NEGOTIABLE. Every code review must check them.

### 1. Tenant ID Source
- `tenant_id` MUST come from **JWT token**, NEVER from request body or URL params
- Use `Depends(get_current_user_with_tenant)` to extract
- NEVER trust client-supplied tenant_id

### 2. Database Queries
- EVERY query that returns tenant-scoped data MUST filter by `tenant_id`
- Use `TenantScopedQuery` base class or explicit `WHERE tenant_id = :tenant_id`
- Test that cross-tenant data leak is impossible

### 3. Camera Naming Convention
- Format: `t{tenant_id}_c{camera_id}` (e.g., `t1_c5`)
- This lets Frigate MQTT events be parsed back to tenant
- NEVER use raw camera names in Frigate config

### 4. Storage Path
- Format: `tenants/{tenant_id}/{resource_type}/{resource_id}`
- Example: `tenants/1/recordings/2026-07-23/camera_5.mp4`
- Example: `tenants/1/snapshots/event_12345.jpg`

### 5. Notification Routing
- Each notification MUST be scoped to tenant
- Cooldown keys: `cooldown:{tenant_id}:{camera_id}:{rule_id}`
- User can ONLY see their own tenant's alerts

---

## Code Standards

### Type Hints (MANDATORY)
```python
# ✅ GOOD
async def get_camera(
    camera_id: int,
    tenant_id: int,
    db: AsyncSession
) -> Camera | None:
    ...

# ❌ BAD (no hints)
async def get_camera(camera_id, tenant_id, db):
    ...
```

### Docstrings (MANDATORY for public functions)
```python
async def generate_frigate_config(
    cameras: list[Camera],
    nodes: list[Node]
) -> str:
    """Generate Frigate YAML config from camera and node data.

    Camera names are prefixed with tenant_id (format: t{tenant_id}_c{camera_id})
    so Frigate MQTT events can be routed back to the correct tenant.

    Args:
        cameras: Active cameras to include in config
        nodes: Frigate nodes that will run this config

    Returns:
        YAML string ready to write to /config/config.yml

    Raises:
        ValueError: If no cameras list is empty
    """
```

### Logging (MANDATORY)
```python
# ✅ GOOD
import structlog
logger = structlog.get_logger()

logger.info("camera_created", camera_id=camera.id, tenant_id=camera.tenant_id)

# ❌ BAD
print(f"Camera {camera.id} created")
```

### Error Handling (MANDATORY)
```python
# ✅ GOOD (specific exceptions, structured logging, re-raise)
try:
    result = await frigate_client.reload_config(node_id)
except FrigateConnectionError as e:
    logger.error("frigate_reload_failed", node_id=node_id, error=str(e))
    raise HTTPException(503, "Camera service temporarily unavailable")
except FrigateTimeoutError as e:
    logger.warning("frigate_reload_timeout", node_id=node_id, timeout_sec=30)
    raise HTTPException(504, "Camera service timeout")

# ❌ BAD (swallow all exceptions)
try:
    result = await frigate_client.reload_config(node_id)
except Exception:
    pass
```

### Async vs Sync
- **Async** for I/O (DB, HTTP, MQTT, Redis, file)
- **Sync** for CPU-bound (image processing, model inference)
- For inference, use `asyncio.to_thread()` to wrap sync code

### Constants (NO magic numbers)
```python
# ✅ GOOD
DEFAULT_TENANT_CAMERA_LIMIT = 100
ALERT_COOLDOWN_SECONDS = 300
FRIGATE_RELOAD_TIMEOUT_SEC = 30
JWT_EXPIRATION_HOURS = 24

# ❌ BAD
if camera_count > 100:  # Why 100?
    await redis.set(key, 1, ex=300)  # Why 300?
```

---

## Naming Conventions

### Files and Modules
- **snake_case**: `camera_service.py`, `event_router.py`
- **No capitals**: `frigateclient.py` ❌ → `frigate_client.py` ✅

### Classes
- **PascalCase**: `CameraService`, `FrigateClient`, `EventRouter`
- **No abbreviations**: `CamSvc` ❌ → `CameraService` ✅

### Functions and Variables
- **snake_case**: `get_camera_by_id()`, `tenant_id`, `rtsp_url`
- **Booleans**: `is_active`, `has_access`, `should_notify`
- **Private**: prefix with `_`: `_internal_helper()`

### Database Tables
- **snake_case, plural**: `tenants`, `cameras`, `events`, `nodes`
- **Foreign keys**: `{table_singular}_id`: `tenant_id`, `camera_id`

### API Endpoints
- **Plural nouns**: `/api/cameras`, `/api/events`
- **HTTP methods**: GET (read), POST (create), PUT (update), DELETE (delete)
- **Kebab-case in URLs**: `/api/cameras/{camera_id}/snapshot`

---

## Testing (MANDATORY)

### Test Structure
```
tests/
├── unit/
│   ├── test_camera_service.py
│   └── test_event_router.py
├── integration/
│   ├── test_camera_api.py
│   └── test_frigate_integration.py
└── conftest.py  # shared fixtures
```

### Test Naming
```python
# ✅ GOOD (descriptive)
async def test_create_camera_with_invalid_rtsp_url_returns_400():
    ...

async def test_event_router_drops_duplicate_alerts_within_cooldown():
    ...

# ❌ BAD (vague)
async def test_camera():
    ...

async def test_1():
    ...
```

### Coverage Requirements
- **Business logic**: 90%+ coverage
- **API endpoints**: 80%+ coverage
- **Critical paths (auth, tenant isolation)**: 100% coverage

### What to Test
- ✅ Happy path (正常情況)
- ✅ Error cases (4xx, 5xx)
- ✅ Edge cases (empty input, max boundary)
- ✅ Tenant isolation (CRITICAL - test cross-tenant attack scenarios)

---

## Git Workflow (MANDATORY)

### Branch Strategy
```
main              ← Production code, always deployable
develop           ← Integration branch
feat/xxx          ← New feature
fix/xxx           ← Bug fix
refactor/xxx      ← Code refactor
docs/xxx          ← Documentation only
```

### Commit Messages (Conventional Commits)
```
feat: add multi-tenant camera CRUD API
fix: handle MQTT reconnection in event router
refactor: extract tenant isolation to middleware
docs: update ARCHITECTURE.md with new schema
test: add integration tests for notification flow
chore: upgrade FastAPI to 0.110
perf: add Redis cache for camera list query
```

### Pre-Commit Checklist (MANDATORY)
```bash
# Run before EVERY commit
pytest tests/ -v                    # All tests pass
ruff check portal/ edge-agent/ event-router/ ai-backend/   # Linter
mypy portal/ edge-agent/ event-router/ ai-backend/         # Type check
git diff --check                    # No whitespace errors
```

---

## FORBIDDEN (DO NOT DO)

These are explicitly forbidden to prevent over-engineering and scope creep.

### Architecture
- ❌ **Kubernetes** (Phase 1-2 - use Docker Compose)
- ❌ **NVIDIA DeepStream** (Phase 1-3 - too complex for 1 person)
- ❌ **Next.js** (use FastAPI + Jinja2 for now)
- ❌ **Microservices** (Phase 1 = monolith, split Phase 4)
- ❌ **Multi-region deployment** (Phase 4+)
- ❌ **Custom auth** (use fastapi-users library)
- ❌ **Custom notification framework** (use n8n)
- ❌ **Custom dashboard framework** (use Grafana)

### Code Patterns
- ❌ **Premature optimization** (no caching unless measured needed)
- ❌ **Premature abstraction** (no interfaces until 2nd use case)
- ❌ **Circular imports** (refactor instead)
- ❌ **Global state** (use dependency injection)
- ❌ **SQL injection** (always use SQLAlchemy ORM)
- ❌ **Hardcoded secrets** (use environment variables + .env)

### Scope
- ❌ **Plugin development** (Phase 3+)
- ❌ **Model registry** (Phase 3+)
- ❌ **Failover orchestration** (Phase 3+)
- ❌ **Billing system** (Phase 3+)
- ❌ **Multi-language i18n** (Phase 3+)
- ❌ **Mobile app** (Phase 4+)

---

## Workflow with AI Assistant

### How to Prompt (Best Practice)

**✅ GOOD prompt** (specific, scoped, with context):
```
I need to add camera CRUD API endpoints.

Requirements:
- Multi-tenant scoped (tenant_id from JWT)
- Endpoints: POST/GET/PUT/DELETE /api/cameras
- Use SQLAlchemy 2.0 async
- Alembic migration included
- Pytest tests included

Do NOT:
- Add caching
- Add WebSocket (Phase 2)
- Add rate limiting (Phase 2)

When done:
- Run pytest + ruff + mypy
- Show me the diff summary
```

**❌ BAD prompt** (vague, mega-task, no limits):
```
Build me a multi-tenant camera management system with auth,
notifications, dashboard, analytics, billing, and AI integration.
```

### Review Rules

After AI writes code, you MUST:
1. **Read the diff** - Don't blindly trust
2. **Run tests** - Verify it works
3. **Check tenant isolation** - CRITICAL security check
4. **Ask "why"** - If you don't understand, ask AI to explain
5. **Run linter** - ruff + mypy must pass

### One Task at a Time

- ✅ One feature per AI session
- ✅ Max ~500 lines changed per session
- ✅ Test before moving to next feature
- ❌ Don't ask AI to write 1000+ lines in one shot

---

## File Organization

```
virex/
├── CLAUDE.md                    ← YOU ARE HERE (AI reads this)
├── ARCHITECTURE.md              ← System design (human + AI reads)
├── ROADMAP.md                   ← 6-week phase plan
├── README.md                    ← Project intro for new devs
├── LICENSE                      ← MIT License
├── .gitignore
├── .env.example                 ← Template for environment variables
├── docker-compose.yml           ← Full stack definition
├── docker-compose.dev.yml       ← Dev overrides
│
├── portal/                      ← FastAPI web portal (Control Plane)
│   ├── api/
│   │   ├── v1/
│   │   │   ├── endpoints/
│   │   │   │   ├── auth.py
│   │   │   │   ├── cameras.py
│   │   │   │   ├── tenants.py
│   │   │   │   └── events.py
│   │   │   └── router.py
│   │   └── deps.py              ← Auth dependencies
│   ├── core/
│   │   ├── config.py            ← Settings (pydantic-settings)
│   │   ├── security.py          ← JWT, password hashing
│   │   └── database.py          ← SQLAlchemy async session
│   ├── models/                  ← SQLAlchemy ORM
│   │   ├── tenant.py
│   │   ├── user.py
│   │   ├── camera.py
│   │   ├── event.py
│   │   ├── node.py
│   │   └── notification.py
│   ├── schemas/                 ← Pydantic schemas
│   ├── services/                ← Business logic
│   ├── templates/               ← Jinja2 templates
│   ├── static/                  ← CSS, JS, images
│   ├── alembic/                 ← DB migrations
│   │   ├── versions/
│   │   └── env.py
│   ├── main.py                  ← FastAPI app entry
│   ├── pyproject.toml
│   └── tests/
│       ├── unit/
│       └── integration/
│
├── edge-agent/                  ← Python daemon on RTX 4070 PC
│   ├── src/
│   │   ├── heartbeat.py
│   │   ├── config_pull.py
│   │   └── frigate_reload.py
│   ├── main.py
│   ├── pyproject.toml
│   └── tests/
│
├── event-router/                ← MQTT → notification routing
│   ├── src/
│   │   ├── mqtt_consumer.py
│   │   ├── rule_matcher.py
│   │   ├── notification_dispatcher.py
│   │   └── ocr_worker.py
│   ├── main.py
│   ├── pyproject.toml
│   └── tests/
│
├── ai-backend/                  ← AI inference abstraction layer
│   ├── models/                  ← BaseModel abstractions
│   │   ├── base.py
│   │   ├── yolov8.py
│   │   ├── depth_anything.py
│   │   └── mobile_sam.py
│   ├── adapters/                ← Stream input + output
│   │   ├── input/
│   │   │   ├── rtsp.py
│   │   │   ├── hls.py
│   │   │   └── file.py
│   │   └── output/
│   │       ├── json_http.py
│   │       ├── mqtt.py
│   │       └── database.py
│   ├── registry/
│   │   └── model_registry.py
│   ├── main.py
│   ├── pyproject.toml
│   └── tests/
│
├── shared/                      ← Shared types/utilities
│   ├── types/
│   │   ├── detection.py         ← StandardDetectionResult
│   │   └── events.py
│   └── utils/
│       └── logger.py
│
├── deploy/                      ← Deployment configs
│   ├── vps/
│   │   ├── docker-compose.yml
│   │   └── Caddyfile
│   ├── edge-4070/
│   │   ├── docker-compose.yml
│   │   └── frigate/
│   │       └── config.yml
│   └── tailscale/
│       └── setup.md
│
├── docs/
│   ├── API.md
│   ├── DEPLOYMENT.md
│   ├── TROUBLESHOOTING.md
│   ├── ARCHITECTURE_DETAILS.md
│   └── PHASE_1_SUMMARY.md
│
└── scripts/
    ├── init_db.py
    ├── seed_data.py
    └── health_check.sh
```

---

## Quick Reference for AI

### Most Important Rules (TOP 5)

1. **Multi-tenant isolation**: ALWAYS filter by tenant_id from JWT, never from request
2. **Camera naming**: `t{tenant_id}_c{camera_id}` - never raw names
3. **No premature optimization**: write simple code first, optimize later
4. **Test before commit**: pytest + ruff + mypy must all pass
5. **One task at a time**: max 500 lines per AI session

### Common Patterns

**Tenant-scoped query**:
```python
async def get_camera(camera_id: int, current_user: AuthUser, db: AsyncSession) -> Camera:
    result = await db.execute(
        select(Camera)
        .where(Camera.id == camera_id, Camera.tenant_id == current_user.tenant_id)
    )
    camera = result.scalar_one_or_none()
    if not camera:
        raise HTTPException(404, "Camera not found")
    return camera
```

**Frigate config generation**:
```python
def generate_camera_name(tenant_id: int, camera_id: int) -> str:
    return f"t{tenant_id}_c{camera_id}"
```

**MQTT event parsing**:
```python
def parse_camera_name(frigate_name: str) -> tuple[int, int]:
    # "t1_c5" → (1, 5)
    parts = frigate_name.split("_")
    tenant_id = int(parts[0][1:])  # strip 't'
    camera_id = int(parts[1][1:])  # strip 'c'
    return tenant_id, camera_id
```

---

## Questions?

If you're an AI assistant and something is unclear:
1. Read `ARCHITECTURE.md` for system design
2. Read `ROADMAP.md` for current phase priorities
3. Read `docs/API.md` for API contracts
4. Read existing code in the relevant directory
5. **Ask the user** - don't guess on critical decisions

If you're a human developer:
- See `docs/` for detailed guides
- See `README.md` for project intro
- Open an issue or discussion

---

**Last Updated**: 2026-07-23
**Phase**: 1 (MVP)
**Status**: Initial setup