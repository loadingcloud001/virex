# ROADMAP.md - 6-Week Phase Plan

> ⚠️ **STATUS (2026-07-27)**: This file documents the **original 6-week
> Phase 1 MVP plan** (Frigate + go2rtc + PostgreSQL + n8n, 4 cameras by
> Week 6). It is **historical** — the v1 pilot that actually shipped is
> the MediaMTX + per-camera-worker + edge-agent stack described in
> [`README.md`](README.md). A new roadmap covering the current v1
> pilot and the path to Phase 2 is on the to-do list.
>
> Reading this file for current priorities is misleading — refer to
> `docs/` for module-level status, and the issue tracker for active work.

> **Living document**. Updated weekly as progress is made.
> Read by AI assistants (Claude Code) at session start to understand current priority.

---

## Overview

**Phase 1 Goal**: Single-customer pilot (4 cameras) running on production stack, ready for real-world testing by Week 6.

**Success Criteria**:
- Customer can sign up, add 4 cameras via portal
- Live view works (WebRTC)
- Detection events trigger alerts (Telegram)
- Recordings saved for 7 days
- 99% uptime over 2-week observation period

---

## Week-by-Week Plan

### Week 1: Infrastructure Foundation

**Goal**: VPS + database + MQTT broker running, basic FastAPI skeleton

**Deliverables**:
- [ ] Hetzner VPS provisioned (Ubuntu 24.04, 4GB RAM)
- [ ] Tailscale installed on VPS
- [ ] Docker Compose up: PostgreSQL, Redis, MinIO, Mosquitto, Caddy
- [ ] FastAPI app skeleton with health check endpoint
- [ ] Initial Alembic migration setup
- [ ] `.env.example` with all required variables

**Tasks**:
1. Provision VPS via Hetzner Cloud
2. Install Docker + Docker Compose
3. Write `deploy/vps/docker-compose.yml`
4. Write `portal/main.py` (minimal FastAPI app)
5. Test connectivity: VPS localhost → all services
6. Setup Tailscale on VPS, get auth key
7. Document setup in `docs/DEPLOYMENT.md`

**Definition of Done**:
- `curl https://yourdomain.com/health` returns 200
- All services visible via `docker ps`
- PostgreSQL accessible via `psql` from VPS

---

### Week 2: Database + Auth Foundation

**Goal**: User can register, login, get JWT token

**Deliverables**:
- [ ] PostgreSQL schema implemented (tenants, users)
- [ ] Alembic initial migration
- [ ] `fastapi-users` integration
- [ ] JWT token issuance
- [ ] Auth endpoints: `/api/auth/register`, `/api/auth/login`
- [ ] Protected route example
- [ ] Unit tests for auth flow

**Tasks**:
1. Define SQLAlchemy ORM models (Tenant, User)
2. Create Alembic migrations
3. Setup fastapi-users with JWT strategy
4. Write `/api/auth/*` endpoints
5. Add dependency `get_current_user_with_tenant`
6. Write pytest fixtures for test database
7. Test: register → login → protected endpoint

**Definition of Done**:
- New user can register via API
- Login returns valid JWT
- Protected endpoint returns 401 without token
- Protected endpoint returns user data with token
- 5+ unit tests passing

---

### Week 3: Camera Management + Frigate Integration

**Goal**: User can add camera via API, Edge Agent picks up config, Frigate loads it

**Deliverables**:
- [ ] Camera CRUD API endpoints
- [ ] Camera ORM model + migration
- [ ] Frigate config generator (Python)
- [ ] Edge Agent v1: heartbeat + config pull
- [ ] RTF Gate PC setup (Ubuntu 22.04 + Docker + NVIDIA)
- [ ] End-to-end test: add camera → Frigate pulls RTSP

**Tasks**:
1. Camera model + Alembic migration
2. `/api/cameras` CRUD endpoints (tenant-scoped)
3. Frigate config generator function
4. Edge Agent skeleton (heartbeat loop)
5. Edge Agent config pull loop
6. Deploy Frigate on 4070 PC (manually for now)
7. Test: POST camera → wait 60s → check Frigate config

**Definition of Done**:
- POST /api/cameras creates camera in DB
- Within 90s, Edge Agent picks up new config
- Frigate successfully pulls RTSP stream
- Can view stream via Frigate web UI (not yet via portal)

---

### Week 4: Live View + Event Capture

**Goal**: User can see live view in portal, events captured to DB

**Deliverables**:
- [ ] Portal UI: login page + dashboard + camera list
- [ ] Live view via Frigate WebRTC iframe
- [ ] Event Router: MQTT consumer → DB
- [ ] Event list page in portal
- [ ] Snapshot download endpoint
- [ ] Basic Jinja2 templates + Tailwind CSS

**Tasks**:
1. Setup Jinja2 templates with Tailwind
2. Write login page + form handler
3. Write dashboard page (camera grid)
4. Embed Frigate WebRTC via iframe (with auth proxy)
5. Event Router daemon: subscribe MQTT, save to DB
6. Write `/api/events` endpoint
7. Write event list template
8. Test: trigger detection → see event in portal

**Definition of Done**:
- User can log in to portal
- Camera grid shows all cameras with live view
- When person detected, event appears in event list within 5s
- Snapshot download works

---

### Week 5: Alert Rules + Notifications

**Goal**: User can configure alert rules, receive Telegram notifications

**Deliverables**:
- [ ] Alert rules CRUD API
- [ ] Alert rule UI (configure zones, schedule, cooldown)
- [ ] n8n workflow: webhook → Telegram
- [ ] Event Router: rule matching + n8n trigger
- [ ] Redis-based cooldown
- [ ] Notification preferences per user

**Tasks**:
1. AlertRule model + migration
2. `/api/alert-rules` CRUD endpoints
3. Rule matcher logic (label, zone, schedule)
4. Redis cooldown implementation
5. Setup n8n on VPS, configure Telegram bot
6. n8n workflow: receive webhook → send Telegram message
7. Event Router: trigger n8n on matched event
8. Test: rule match → Telegram message received

**Definition of Done**:
- User can create alert rule via portal UI
- When rule matches, Telegram message sent within 10s
- No duplicate messages within cooldown period
- Notifications logged in DB

---

### Week 6: Polish + Pilot Customer Onboarding

**Goal**: First customer onboarded, monitoring in place, ready for production

**Deliverables**:
- [ ] Recording storage verified (MinIO + local on 4070 PC)
- [ ] Playback functionality (event clip viewing)
- [ ] System monitoring (Grafana dashboards)
- [ ] Error handling + logging review
- [ ] Documentation: user guide + admin guide
- [ ] First customer onboarded (4 cameras)

**Tasks**:
1. Configure Frigate recording to MinIO + local backup
2. Build event playback UI (timeline scrubber)
3. Setup Prometheus + Grafana
4. Add basic Grafana dashboards (system health, per-tenant)
5. Review all error handling, add missing logs
6. Write `docs/USER_GUIDE.md`
7. Write `docs/ADMIN_GUIDE.md`
8. Onboard pilot customer, test end-to-end

**Definition of Done**:
- Customer onboarded with 4 cameras
- All features working: live view, detection, alerts, recordings
- System uptime >99% during 7-day observation
- Customer can self-serve basic operations

---

## Beyond Week 6 (Phase 2 Preview)

**Not in Phase 1 scope, but documented for future reference:**

- Multi-node scheduler (when >100 cameras)
- 1060 PC as warm standby
- OCR worker (llama.cpp + dots.ocr)
- Segmentation overlay (SAM)
- Plugin ecosystem
- Billing system
- Multi-region deployment
- Failover automation

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Frigate config reload breaks live streams | High | Test reload separately, accept 5-30s downtime |
| Customer RTSP URL doesn't work | Medium | Provide testing endpoint, fallback to push agent (Phase 2) |
| VPS bandwidth insufficient | Medium | Monitor, upgrade to CX32 if needed (€8/month) |
| GPU overheating | Medium | Monitor with nvidia-smi, alerts >85°C |
| PostgreSQL corruption | High | Daily automated backups to separate volume |
| Single point of failure (VPS) | Medium | Phase 2: backup VPS, automated failover |

---

## Progress Tracking

### Week 1 Status: ⏳ Not Started
- [ ] Infrastructure
- [ ] Database
- [ ] Auth
- ...

### Week 2 Status: ⏳ Not Started
- ...

(Updated weekly by marking completed items with ✅)

---

**Last Updated**: 2026-07-23
**Phase**: 1 (MVP)
**Current Week**: 1