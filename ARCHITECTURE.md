# ARCHITECTURE.md - System Design

> ⚠️ **STATUS (2026-07-27)**: This file is **historical** — written for the
> earlier Phase 1 MVP design (Frigate + go2rtc + n8n + PostgreSQL on Hetzner).
> It has **not** been rewritten for the current v1 pilot. The current design
> is described in the [`README.md`](README.md) module table and in
> [`docs/`](docs/) (especially `docs/mediamtx-architecture.md`,
> `docs/config-hot-reload.md`, `docs/ai-pipeline.md`).
>
> Treat the body of this file as a **design rationale document for the
> Phase 1 era** — useful for understanding *why* certain tradeoffs (e.g.
> why we picked MediaMTX over go2rtc, why we ship per-camera worker
> containers rather than sharing one process) were made. Do **not** rely
> on it for current module structure, schema, or deployment topology.
>
> A full rewrite is on the roadmap; for now, refer to `README.md` for the
> authoritative current state.

---

## Current Architecture (Summary — see README.md for the canonical version)

```
┌─────────────────────────────────────────────────────────────────┐
│  Customer Site (Hikvision / Dahua Camera)                       │
│  RTSP stream outbound                                           │
└────────────────────────┬────────────────────────────────────────┘
                         │ RTSP
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  RTX 4070 PC (Ubuntu 22.04) — Edge                              │
│                                                                 │
│  ┌────────────────────────────────────────────────────────┐     │
│  │ MediaMTX (Docker)                                      │     │
│  │ ├─ RTSP/WebRTC/HLS ingest                              │     │
│  │ ├─ Per-segment MP4 recording (30s segments)            │     │
│  │ └─ REST control API (/v3/paths/list)                    │     │
│  └────────────────────────────────────────────────────────┘     │
│  ┌────────────────────────────────────────────────────────┐     │
│  │ FFmpeg transcoder sidecars (one per camera)            │     │
│  │ └─ Repack camera-native stream → H.264 *_h264 path      │     │
│  └────────────────────────────────────────────────────────┘     │
│  ┌────────────────────────────────────────────────────────┐     │
│  │ Per-camera worker containers (one per camera)          │     │
│  │ ├─ RTSP feeder (PyAV) → Motion → Zone → Detect → Seg   │     │
│  │ ├─ MQTT publisher (detection events)                   │     │
│  │ ├─ Snapshot uploader (MinIO/S3, signed URLs)           │     │
│  │ └─ FastAPI admin (per-worker :32000+N port)            │     │
│  └────────────────────────────────────────────────────────┘     │
│  ┌────────────────────────────────────────────────────────┐     │
│  │ Clip-builder (one container, all cameras)              │     │
│  │ └─ MQTT-triggered FFmpeg cuts → MinIO                   │     │
│  └────────────────────────────────────────────────────────┘     │
│  ┌────────────────────────────────────────────────────────┐     │
│  │ Edge agent (virex-edge-agent)                          │     │
│  │ ├─ inotify on workers.yaml                             │     │
│  │ ├─ Diff → Tier A/B/C/D classifier                      │     │
│  │ └─ Jinja2 → renders MediaMTX/worker/transcoder compose │     │
│  └────────────────────────────────────────────────────────┘     │
│  ┌────────────────────────────────────────────────────────┐     │
│  │ Mosquitto MQTT broker (127.0.0.1:1883)                 │     │
│  └────────────────────────────────────────────────────────┘     │
└────────────────────────┬────────────────────────────────────────┘
                         │ MQTT (Tailscale / Cloudflare tunnel in prod)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Event Router                                                   │
│  ├─ MQTT subscribe → per-tenant rule match → notification sink │
│  └─ Webhook / Telegram / Email / n8n (Phase 2)                  │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Storage                                                        │
│  ├─ Tencent COS bucket `virex-snapshots-1308927282` (ap-singapore) │
│  │  └─ Custom domain `snapshots.loadingtechnology.app`         │
│  │     via Cloudflare Origin CA                                │
│  └─ Local MediaMTX recordings (per-camera, 30s segments)        │
└─────────────────────────────────────────────────────────────────┘
```

**Module ownership**:
- `ai-backend/` — worker + detector + clip-builder (all per-camera containers)
- `edge-agent/` — orchestrator
- `event-router/` — MQTT → sink
- `portal/` — control plane (VPS, Phase 2)

**License**: AGPL-3.0 (umbrella) — see [`LICENSE`](LICENSE).

---

# ARCHITECTURE.md — Historical Phase 1 Content (preserved below)

The rest of this file documents the **original Phase 1 MVP design** that the
project started with. It is kept for design-rationale continuity (so reviewers
can trace *why* the current v1 pilot looks the way it does) and is not
authoritative for current state.

## 1. Product Overview

### What
B2B SaaS multi-tenant CCTV monitoring platform with centralized AI processing.

### Who
- **Primary user**: B2B customers (construction sites, retail, parking lots)
- **Customer onboarding**: Self-service via web portal
- **Operator**: 1 founder + 1-2 maintenance engineers (after Phase 3)

### Why
- Customers don't want to buy/maintain NVR hardware
- AI processing centralized = lower cost per camera
- Multi-tenant = single deployment serves many customers
- WebRTC live view + instant alerts = better UX than legacy NVR

### Scale Target

| Phase | Timeline | Cameras | Customers | GPU Nodes |
|-------|----------|---------|-----------|-----------|
| Phase 1 | Month 1-6 | 0 → 50 | 1 (pilot) | 1 (RTX 4070) |
| Phase 2 | Month 6-12 | 50 → 200 | 5-10 | 1-2 |
| Phase 3 | Month 12-18 | 200 → 1000 | 20-30 | 3-5 (cluster) |
| Phase 4 | Month 18+ | 1000+ | 50+ | Multi-region |

---

## 2. High-Level Architecture

### Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  Customer Site (Hikvision / Dahua Camera)                       │
│  RTSP stream outbound to control plane                          │
└────────────────────────┬────────────────────────────────────────┘
                         │ RTSP / WebRTC
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  RTX 4070 PC (Ubuntu 22.04) — AI Edge                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Frigate (Docker)                                        │   │
│  │  ├─ go2rtc: RTSP ingest + WebRTC relay                   │   │
│  │  ├─ Detect: YOLOv8 (person/vehicle/animal)               │   │
│  │  ├─ Record: MP4 segments, HLS playback                   │   │
│  │  └─ MQTT event output → frigate/events                   │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Edge Agent (Python daemon)                              │   │
│  │  ├─ Heartbeat → Control Plane (every 30s)                │   │
│  │  ├─ Config Pull → Fetch per-tenant Frigate YAML         │   │
│  │  └─ Reload → Trigger Frigate /api/config/restart         │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  OCR Worker (optional, Phase 2)                          │   │
│  │  - MQTT subscribe Frigate events                         │   │
│  │  - Pull snapshot → llama.cpp + dots.ocr                  │   │
│  │  - Write result to PostgreSQL                            │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Local MinIO + HDD (recording storage)                   │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │ Tailscale WireGuard (encrypted tunnel)
                         │ MQTT events + HTTP config
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Hetzner VPS (Ubuntu 24.04, 4GB RAM) — Control Plane             │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Web Layer                                              │   │
│  │  ├─ Caddy: HTTPS reverse proxy (auto SSL)                │   │
│  │  └─ FastAPI Portal: REST API + Jinja2 UI               │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Data Layer                                             │   │
│  │  ├─ PostgreSQL: tenants, cameras, rules, events, nodes   │   │
│  │  ├─ Redis: session, cooldown, rate limit                 │   │
│  │  └─ MinIO: video recordings, snapshots                  │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Event Processing                                       │   │
│  │  ├─ Mosquitto MQTT broker                                │   │
│  │  ├─ Event Router: MQTT → tenant → rule → notify         │   │
│  │  └─ Notification: n8n → Telegram / Email / Webhook       │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Monitoring                                             │   │
│  │  ├─ Prometheus: metrics collection                       │   │
│  │  └─ Grafana: system + tenant dashboards                 │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Logical Layers (Phase 1-2: Logic-only, physical separation later)

```
Layer 1 - Ingestion:  RTSP → decoded frames      [Frigate + go2rtc]
Layer 2 - Compute:    Frames → AI detections      [Frigate + YOLOv8]
Layer 3 - Control:    Detections → business logic [Your code]
Layer 4 - Storage:    Business state → DB         [PostgreSQL + MinIO]
Layer 5 - Delivery:   Alerts → user channels     [n8n + Telegram]
```

**Phase 3 separation**: If scaling requires, Layer 1+2 move to dedicated GPU cluster, Layer 3+4+5 stay on VPS. Communication via Kafka (not MQTT).

---

## 3. Component Details

### 3.1 Frigate (AI Edge)

**Why chosen**:
- Open-source MIT license
- Built-in go2rtc (RTSP + WebRTC)
- Built-in YOLOv8 support
- Built-in MQTT event output
- Built-in MP4 recording + HLS playback
- Active community (16k+ GitHub stars)
- 1-person maintainable

**Configuration source**:
- Static `config.yml` per node (Phase 1)
- Dynamically generated from PostgreSQL via Edge Agent
- Hot-reload via Frigate API `/api/config/restart` (~5-30s downtime)

**Multi-tenant handling**:
- Camera names prefixed: `t{tenant_id}_c{camera_id}`
- MQTT event includes camera name → parse to (tenant_id, camera_id)
- No native multi-tenant concept (handled by Control Plane)

### 3.2 Edge Agent (on RTX 4070 PC)

**Responsibilities**:
1. **Heartbeat**: Every 30s, report to Control Plane
   - GPU utilization (nvidia-smi)
   - CPU + RAM (psutil)
   - Active camera count
   - Frigate health status
2. **Config Pull**: Every 60s, fetch latest config version from Control Plane
   - If new version, write to `/config/config.yml`
   - Trigger Frigate reload via API
3. **Self-healing**: If Frigate crashes, restart container

**Deployment**: systemd service on Ubuntu 22.04

### 3.3 Control Plane (VPS)

#### 3.3.1 FastAPI Portal

**Tech**: FastAPI 0.110+, SQLAlchemy 2.0 async, Pydantic v2

**Modules**:
```
portal/
├── api/v1/endpoints/    ← REST endpoints
├── core/                ← Config, security, DB session
├── models/              ← SQLAlchemy ORM
├── schemas/             ← Pydantic request/response
├── services/            ← Business logic
├── templates/           ← Jinja2 HTML
└── tests/
```

**API endpoints** (Phase 1):
- `POST /api/auth/login` - JWT issuance
- `POST /api/auth/register` - New tenant signup
- `GET /api/cameras` - List tenant's cameras
- `POST /api/cameras` - Add new camera (triggers Frigate config update)
- `GET /api/cameras/{id}` - Get camera details
- `PUT /api/cameras/{id}` - Update camera config
- `DELETE /api/cameras/{id}` - Remove camera
- `GET /api/events` - List alert events
- `GET /api/events/{id}/snapshot` - Get event snapshot
- `GET /api/events/{id}/clip` - Get event video clip
- `GET /api/tenants/current` - Get current tenant info
- `PUT /api/tenants/current` - Update tenant settings
- `GET /api/nodes` - List Frigate nodes (admin only)

#### 3.3.2 PostgreSQL Schema

**Core tables** (Phase 1):

```sql
-- Tenants (organizations)
CREATE TABLE tenants (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    subdomain VARCHAR(63) UNIQUE NOT NULL,
    branding JSONB DEFAULT '{}',  -- logo, color, company_name
    features JSONB DEFAULT '{}',  -- ocr, face_recog, etc.
    limits JSONB DEFAULT '{}',   -- max_cameras, retention_days
    status VARCHAR(20) DEFAULT 'active',  -- active, suspended, trial
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Users (multi-tenant, scoped)
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) DEFAULT 'member',  -- admin, member, viewer
    notification_channels JSONB DEFAULT '{}',  -- telegram_chat_id, email
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login_at TIMESTAMP WITH TIME ZONE,
    UNIQUE(tenant_id, email)
);

CREATE INDEX idx_users_tenant ON users(tenant_id);

-- Frigate Nodes (GPU servers)
CREATE TABLE nodes (
    id SERIAL PRIMARY KEY,
    hostname VARCHAR(255) UNIQUE NOT NULL,
    tailscale_ip VARCHAR(45) NOT NULL,
    max_cameras INTEGER DEFAULT 80,
    gpu_model VARCHAR(100),
    status VARCHAR(20) DEFAULT 'pending',  -- pending, healthy, unhealthy, offline
    last_heartbeat_at TIMESTAMP WITH TIME ZONE,
    current_config_version INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Cameras
CREATE TABLE cameras (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    node_id INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    name VARCHAR(255) NOT NULL,
    location VARCHAR(255),
    rtsp_url TEXT NOT NULL,
    frigate_name VARCHAR(63) NOT NULL,  -- t{tenant_id}_c{camera_id}
    status VARCHAR(20) DEFAULT 'active',  -- active, disabled, offline
    zones JSONB DEFAULT '[]',  -- [{name, polygon, object_filter}]
    object_filters JSONB DEFAULT '{}',  -- {person: {min_score: 0.6}, ...}
    recording_enabled BOOLEAN DEFAULT TRUE,
    retention_days INTEGER DEFAULT 7,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(tenant_id, name)
);

CREATE INDEX idx_cameras_tenant ON cameras(tenant_id);
CREATE INDEX idx_cameras_node ON cameras(node_id);
CREATE INDEX idx_cameras_frigate_name ON cameras(frigate_name);

-- Alert Rules
CREATE TABLE alert_rules (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    camera_id INTEGER REFERENCES cameras(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    object_labels TEXT[],  -- ['person', 'vehicle']
    zones TEXT[],  -- zone names to monitor
    schedule JSONB DEFAULT '{}',  -- {days: [0,1,2,3,4,5,6], hours: {start: 22, end: 6}}
    cooldown_seconds INTEGER DEFAULT 300,
    notification_channels JSONB DEFAULT '[]',  -- ['telegram', 'email']
    notify_on JSONB DEFAULT '{"enter": true, "exit": false}',
    is_enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_alert_rules_tenant ON alert_rules(tenant_id);
CREATE INDEX idx_alert_rules_camera ON alert_rules(camera_id);

-- Events (alert history)
CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    frigate_event_id VARCHAR(100),  -- Frigate's internal event ID
    label VARCHAR(50) NOT NULL,
    score FLOAT,
    bbox JSONB,  -- [x, y, w, h]
    zone VARCHAR(100),
    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP WITH TIME ZONE,
    snapshot_url TEXT,  -- MinIO path
    clip_url TEXT,  -- MinIO path
    extra_data JSONB DEFAULT '{}',  -- OCR result, attributes
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_events_tenant ON events(tenant_id);
CREATE INDEX idx_events_camera ON events(camera_id);
CREATE INDEX idx_events_start_time ON events(start_time DESC);

-- Notification Log
CREATE TABLE notifications (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    rule_id INTEGER REFERENCES alert_rules(id) ON DELETE SET NULL,
    channel VARCHAR(50) NOT NULL,  -- telegram, email, webhook
    status VARCHAR(20) NOT NULL,  -- sent, failed, pending
    error_message TEXT,
    sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_notifications_tenant ON notifications(tenant_id);
CREATE INDEX idx_notifications_event ON notifications(event_id);
```

#### 3.3.3 Event Router

**Flow**:
```
1. MQTT subscribe: frigate/events
2. Parse camera_name → (tenant_id, camera_id)
3. Lookup camera in PostgreSQL (verify exists)
4. Lookup alert_rules for this camera
5. For each matching rule:
   a. Check cooldown (Redis SET key NX EX)
   b. If first within cooldown:
      - Save event to PostgreSQL
      - Save snapshot to MinIO
      - Trigger notification (n8n webhook)
6. (Optional) Trigger OCR worker
```

**Code skeleton**:
```python
# event-router/src/mqtt_consumer.py
import paho.mqtt.client as mqtt
import asyncio

class MQTTConsumer:
    def __init__(self, db, redis, n8n_client):
        self.db = db
        self.redis = redis
        self.n8n = n8n_client
    
    async def on_message(self, topic, payload):
        event = json.loads(payload)
        camera_name = event["after"]["camera"]  # "t1_c5"
        
        tenant_id, camera_id = parse_camera_name(camera_name)
        if not tenant_id:
            return
        
        # Verify camera belongs to tenant
        camera = await self.db.get_camera(camera_id, tenant_id)
        if not camera:
            return
        
        # Match rules
        rules = await self.db.get_active_rules(tenant_id, camera_id, event["after"]["label"])
        
        for rule in rules:
            if not self.match_rule(rule, event):
                continue
            
            # Cooldown check
            cooldown_key = f"cooldown:{tenant_id}:{camera_id}:{rule.id}"
            if not await self.redis.set(cooldown_key, 1, ex=rule.cooldown_seconds, nx=True):
                continue  # Already notified recently
            
            # Save event
            event_id = await self.db.save_event(tenant_id, camera_id, event)
            
            # Trigger notification
            await self.n8n.send(rule.notification_channels, {
                "event_id": event_id,
                "camera": camera.name,
                "label": event["after"]["label"],
                "score": event["after"]["score"],
                "snapshot_url": f"/api/events/{event_id}/snapshot",
                "clip_url": f"/api/events/{event_id}/clip"
            })
```

#### 3.3.4 MinIO (S3-compatible storage)

**Buckets**:
- `tenants` (multi-tenant data)
  - Structure: `tenants/{tenant_id}/recordings/{date}/{camera_frigate_name}/{timestamp}.mp4`
  - Structure: `tenants/{tenant_id}/snapshots/{event_id}.jpg`
  - Structure: `tenants/{tenant_id}/clips/{event_id}.mp4`

**Access**:
- Backend uses pre-signed URLs (no public buckets)
- Portal API generates time-limited URLs for user access

### 3.4 Notification Routing (n8n)

**Why n8n**:
- Visual workflow editor
- 100+ integrations
- Self-hostable
- Phase 1 use: Telegram / Email / Webhook fan-out

**Workflows**:
1. **CCTV Alert Router**: Receive webhook from Event Router → Send to channels
2. **User Signup**: New tenant → Send welcome email + Telegram link
3. **System Health**: Daily report → Admin email

### 3.5 RTX 4070 PC (AI Edge)

**Hardware**:
- GPU: NVIDIA RTX 4070 (12GB VRAM)
- CPU: Any modern x86_64
- RAM: 16GB+ recommended
- Storage: 500GB+ SSD for 7-day recordings

**OS**: Ubuntu 22.04 LTS (NOT Windows, NOT WSL2)

**Software stack**:
```
Docker:
├─ Frigate (ghcr.io/blakeblackshear/frigate:stable)
├─ Edge Agent (custom Python, runs as systemd service)
└─ (Phase 2) OCR Worker (llama.cpp + dots.ocr)

Tailscale: VPN tunnel to VPS
NVIDIA drivers: 535+ with CUDA 12.x
```

**Capacity (Phase 1)**:
- YOLOv8s at 640x640: ~80 FPS sustained
- Sampling 1 frame per 3 seconds per camera: supports ~80 cameras
- First customer (4 cameras): plenty of headroom

**GPU 1060 PC** (Phase 1):
- Not used in production
- Reserved for development testing or cold backup (Phase 2+)

---

## 4. Multi-Tenant Design

### 4.1 Tenant Isolation Strategy

**Database level**:
- Every tenant-scoped table has `tenant_id` FK
- Every query MUST filter by `tenant_id`
- Enforced via dependency injection (`get_current_user_with_tenant`)

**Application level**:
- JWT token contains `tenant_id` (signed, non-forgeable)
- All API endpoints extract `tenant_id` from token, NEVER from URL/body
- Test suite includes cross-tenant attack scenarios

**Storage level**:
- MinIO paths: `tenants/{tenant_id}/...`
- Pre-signed URLs time-limited (15 min)

**Frigate level**:
- Camera names: `t{tenant_id}_c{camera_id}`
- Per-node config contains multiple tenants' cameras (Phase 1-2 single node)
- Event routing parses name → tenant

### 4.2 Tenant Customization (Phase 2)

Per-tenant configuration:
```json
{
  "branding": {
    "logo_url": "https://...",
    "primary_color": "#FF5733",
    "company_name": "Acme Corp"
  },
  "features": {
    "ocr_enabled": true,
    "face_recognition_enabled": false,
    "line_crossing_enabled": true
  },
  "limits": {
    "max_cameras": 100,
    "retention_days": 30,
    "max_users": 20
  }
}
```

### 4.3 Subdomain Routing (Phase 2)

- Pattern: `{subdomain}.yourplatform.com`
- Caddy: subdomain-based routing
- Portal reads subdomain → loads tenant config
- CSS variables for branding

---

## 5. Data Flow Examples

### 5.1 New Camera Onboarding

```
User: opens portal, clicks "Add Camera"
  ↓
Frontend: POST /api/cameras {name, rtsp_url, location}
  ↓
Backend: 
  1. Validate input (Pydantic)
  2. Extract tenant_id from JWT
  3. INSERT INTO cameras
  4. Compute frigate_name = "t{tenant_id}_c{camera_id}"
  5. UPDATE cameras SET frigate_name = ...
  6. Generate new Frigate config (per-node YAML)
  7. Increment node.current_config_version
  8. (Edge Agent polls and picks up new version within 60s)
  ↓
Edge Agent:
  1. Detects new config version
  2. Downloads YAML
  3. Writes to /config/config.yml
  4. POST http://frigate:5000/api/config/restart
  5. Frigate reloads (~5-30s downtime)
  ↓
Frigate:
  1. Reads new config
  2. Starts pulling RTSP stream
  3. Starts YOLOv8 detection
  4. Publishes MQTT events
  ↓
User (in portal):
  Sees live view (WebRTC iframe) within 30-60s
```

### 5.2 Alert Delivery

```
Frigate:
  1. Person detected in zone "entrance"
  2. MQTT publish: frigate/events {camera: "t1_c5", label: "person", ...}
  ↓
Event Router:
  1. Receive MQTT message
  2. Parse "t1_c5" → (tenant_id=1, camera_id=5)
  3. Query DB: alert_rules WHERE camera_id=5 AND object_labels contains 'person'
  4. Match rule found (e.g., "Night intrusion")
  5. Check Redis cooldown: SET "cooldown:1:5:rule_1" NX EX 300
     → First time, succeed
  6. Save event to PostgreSQL
  7. Fetch snapshot from Frigate: GET /api/t1_c5/event/{id}/snapshot.jpg
  8. Upload snapshot to MinIO: tenants/1/snapshots/{event_id}.jpg
  9. POST to n8n webhook with event data + snapshot URL
  ↓
n8n workflow:
  1. Receive webhook
  2. Route to Telegram channel → user receives photo + alert
  3. Route to email if configured
  4. Route to webhook URL if configured
  ↓
User:
  Gets Telegram message within 5-10s of event
  Includes photo + "Camera: Front Door (Tunnel Site) - Person detected"
```

### 5.3 Live View

```
User: opens portal, clicks camera
  ↓
Frontend:
  1. GET /api/cameras/{id}/live_view_url
  2. Backend returns WebRTC URL: https://{node_tailscale}/webcam/{frigate_name}
  3. Frontend <iframe src={url}>
  ↓
Caddy (on VPS):
  Reverse proxy to Frigate via Tailscale tunnel
  ↓
Frigate (go2rtc):
  Serves WebRTC stream directly to browser
  ↓
User:
  Sees live video, <2 second latency
```

---

## 6. Security

### 6.1 Authentication

- **Method**: JWT (HS256 or RS256)
- **Library**: fastapi-users
- **Token expiry**: 24 hours
- **Refresh token**: Optional, Phase 2
- **Password hashing**: bcrypt (via passlib)

### 6.2 Authorization

- **Roles**: admin, member, viewer
- **Admin**: Full tenant control
- **Member**: View + manage own resources
- **Viewer**: Read-only

### 6.3 Network Security

- **VPS ↔ PC**: Tailscale WireGuard (encrypted tunnel)
- **Public HTTPS**: Caddy + Let's Encrypt
- **Database**: NOT exposed to public (VPS internal network only)
- **MinIO**: NOT exposed to public (pre-signed URLs only)
- **MQTT**: NOT exposed to public (Tailscale tunnel only)

### 6.4 RTSP Stream Security

- **Customer camera**: User-controlled credentials in RTSP URL
- **Transport**: Stored encrypted in PostgreSQL (AES-256)
- **TLS support**: RTSP over TLS (optional, Phase 2)

---

## 7. Deployment

### 7.1 VPS (Hetzner CX22)

**Spec**: 4GB RAM, 2 vCPU, 40GB SSD, €4/month

**Stack**:
```yaml
services:
  caddy:        # HTTPS reverse proxy
  portal:       # FastAPI
  postgres:     # PostgreSQL 16
  redis:        # Redis 7
  minio:        # S3-compatible storage
  mqtt:         # Mosquitto MQTT
  event-router: # Python event consumer
  n8n:          # Notification workflow
  prometheus:   # Metrics
  grafana:      # Dashboards
```

### 7.2 Edge PC (RTX 4070)

**OS**: Ubuntu 22.04 LTS

**Install steps**:
1. Install NVIDIA drivers (535+)
2. Install Docker + NVIDIA Container Toolkit
3. Install Tailscale
4. Clone repo
5. Deploy Frigate + Edge Agent (docker-compose)
6. Register node with Control Plane

### 7.3 Tailscale Setup

- 5 minutes to install on both VPS and PC
- No port forwarding needed
- Automatic key exchange
- ACLs limit which devices can connect to which

---

## 8. Monitoring & Observability

### 8.1 Metrics (Prometheus)

- **Portal**: Request rate, error rate, latency (via prometheus-fastapi-instrumentator)
- **Frigate**: Detection count, CPU/GPU util (Frigate built-in exporter)
- **Database**: Query latency, connection count (postgres_exporter)
- **Redis**: Hit rate, memory (redis_exporter)
- **MQTT**: Message rate (mosquitto exporter)

### 8.2 Dashboards (Grafana)

- **System Overview**: All services health
- **Per-Tenant**: Cameras, events, storage usage
- **GPU Node**: Frigate stats, GPU temperature
- **Alerts**: Critical alerts, error rates

### 8.3 Logging

- **Structured JSON logs** (structlog)
- **Centralized**: Phase 2 - Loki or similar
- **Retention**: 30 days

---

## 9. Future Phases (Decision Tree)

### Phase 3 Decisions (when needed)

**Multi-node scheduler** (when >100 cameras):
```python
def assign_camera(camera):
    candidates = [n for n in nodes if n.status == "healthy" and n.active_cams < n.max_cameras]
    return max(candidates, key=lambda n: (
        0.3 * (1 - n.gpu_util) +
        0.3 * (1 - n.cpu_util) +
        0.2 * (1 - n.bandwidth_used) +
        0.2 * n.uptime_score
    ))
```

**Failover** (when 1+ standby nodes):
- Heartbeat monitoring (30s timeout)
- Auto-migrate cameras to standby
- DNS-level rerouting for live view

**Model registry** (when 3+ models):
- YAML-based config (no need for full registry)
- Hot-swap via Frigate API reload

**Plugin ecosystem** (Phase 4):
- Extract Worker abstractions
- Publish to PyPI
- GitHub-based community

---

## 10. Open Questions / Risks

### Technical Risks

| Risk | Mitigation |
|------|-----------|
| Frigate breaks custom config reload | Test with 1 camera before scaling |
| GPU thermal throttling | Monitor with nvidia-smi, alerts >85°C |
| RTSP firewall issues | Use Tailscale + customer NAT config guide |
| MinIO disk full | Alert at 80%, auto-purge old recordings |
| PostgreSQL connection exhaustion | asyncpg pool, monitor via Prometheus |

### Business Risks

| Risk | Mitigation |
|------|-----------|
| Customer churn | Self-service portal, easy migration |
| Competitor (Verkada) | Lower price point, no vendor lock-in |
| Camera compatibility | Support RTSP standard, test with Hikvision/Dahua |
| Scaling before revenue | Phase 1 optimized for 1 customer |

---

## 11. References

- **Frigate docs**: https://docs.frigate.video/
- **FastAPI docs**: https://fastapi.tiangolo.com/
- **SQLAlchemy 2.0**: https://docs.sqlalchemy.org/en/20/
- **Tailscale**: https://tailscale.com/kb/
- **Hetzner Cloud**: https://www.hetzner.com/cloud

---

**Last Updated**: 2026-07-23
**Phase**: 1 (MVP)
**Status**: Initial design