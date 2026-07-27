# Event Router

MQTT consumer that bridges detection events to business logic and
notifications. Runs on the VPS as part of `deploy/vps/docker-compose.yml`.

## What This Module Does

1. **Subscribe** Mosquitto topic `virex/detections` (all worker events).
2. **Cooldown** Redis `SETNX EX 30` keyed on
   `(tenant_id, camera_id, label)` — first event in the 30 s window
   proceeds, the rest are silently dropped (stateless per-frame emit by
   the worker is the source of duplicates).
3. **Persist** INSERT into `events` row (camera-friendly bbox + score).
4. **Match** tenant's `alert_rules`; the matched rule set is sent to n8n.
5. **Publish** `virex/events_created` (carries `event_id` + `event_ts`)
   so the edge-side `clip-builder` can cut and upload a clip.
6. **Dispatch** POST to n8n webhook; n8n routes to Telegram / Email /
   generic webhook per the user's configured channels.

## Settings (env)

| Var | Required | Default |
|---|---|---|
| `DATABASE_URL` | yes | — |
| `REDIS_URL` | no | `redis://default@127.0.0.1:6379/0` |
| `MQTT_BROKER` | yes | — |
| `N8N_WEBHOOK_URL` | no | (skips notification when unset) |

## Wire schema

`virex/detections` → see `ai-backend/worker/schema.py:DetectionEvent`.
`virex/events_created` → `{v:1, event_id, tenant_id, mtx_path, event_ts}`.

## Tests

```bash
pip install -e .[dev]
pytest tests/ -v
ruff check src/
```

## See also

- `ai-backend/` — worker + clip-builder this consumes.
- The implementation plan §6 (event schemas) and §7 Phase D (router tasks).