# MediaMTX Architecture

> **Audience**: engineers integrating, extending, or operating MediaMTX.
> Assumes basic familiarity with streaming protocols (RTSP, WebRTC, HLS) and
> Go's concurrency model.
>
> **Last reviewed**: 2026-07-25 against `bluenviron/mediamtx` v1.19.3.
> Verified against official configuration reference. Restructured into four
> parts; added Hikvision-specific notes; consolidated FFmpeg command examples;
> corrected "5 patterns" → "6 patterns" count. **Correction pass 2026-07-25**:
> fixed recording field names (`segmentDuration` → `recordSegmentDuration`),
> hook names (`runOnReady` → `runOnAvailable`), hook env vars
> (`MTX_READER_IP` → `MTX_READER_TYPE/ID`), Hikvision audio URL, Hikvision
> "high entropy" claim (removed), RTSP error code for codec mismatch
> (clarified), decision tree pattern reference.

## TL;DR

MediaMTX is a **single-binary, MIT-licensed "media router"** that accepts live
streams on any of 10+ protocols (RTSP, RTMP, SRT, WebRTC, HLS, MoQ, RTP,
MPEG-TS) and re-publishes them on any other supported protocol without
touching pixels. It does **not** transcode — that's the integration
boundary. The canonical pattern is MediaMTX + an external FFmpeg/GStreamer
transcode sidecar triggered by hooks.

For the Virex `ai-backend` pipeline specifically:

- **Substrate**: MediaMTX as the stream relay (no fork, no patch)
- **Codec unification**: FFmpeg sidecars triggered by `runOnAvailable` hooks
- **Output format spec**: H.264 High Profile, Level 4.2, yuv420p, CBR,
  4 Mbps, GOP = 50 frames, B-frames disabled
- **Architecture pattern**: dual-layer (raw + normalized) so all consumers
  see a single format, regardless of camera codec

## Table of Contents

- [Part 1 — Fundamentals](#part-1--fundamentals)
  - [What MediaMTX Is](#what-mediamtx-is)
  - [Core Abstractions](#core-abstractions) — Path, Stream
  - [Architecture Diagrams](#architecture-diagrams)
  - [Goroutine & Concurrency Model](#goroutine--concurrency-model)
  - [Protocol Matrix](#protocol-matrix)
- [Part 2 — Configuration & Operations](#part-2--configuration--operations)
  - [Configuration](#configuration)
  - [Authentication](#authentication)
  - [Hooks](#hooks)
  - [Sidecar Integration Patterns](#sidecar-integration-patterns)
  - [Built-in Recording](#built-in-recording)
  - [Control API](#control-api)
  - [Metrics](#metrics)
  - [Scalability Patterns](#scalability-patterns)
- [Part 3 — Codec Compatibility & Transcoding](#part-3--codec-compatibility--transcoding)
  - [Why MediaMTX Doesn't Transcode](#why-mediamtx-doesnt-transcode)
  - [Codec Compatibility Matrix](#codec-compatibility-matrix)
  - [Universal Transcoding Format Spec](#universal-transcoding-format-spec)
  - [Unified-Format Pipeline with Transcode Sidecars](#unified-format-pipeline-with-transcode-sidecars)
  - [Industry Best Practices](#industry-best-practices)
  - [Hikvision-Specific Notes](#hikvision-specific-notes)
- [Part 4 — Summary](#part-4--summary)
  - [Strengths and Limitations](#strengths-and-limitations)
  - [References](#references)

---

# Part 1 — Fundamentals

## What MediaMTX Is

MediaMTX (formerly `rtsp-simple-server`) is a **single-binary, zero-dependency,
MIT-licensed live media server and proxy** written in Go. Its self-described
identity is a **"media router"**: it does not decode pixels, does not run AI,
and does not enforce a storage policy. Its job is to accept a live stream on
one protocol and re-publish it on any other supported protocol with the lowest
possible latency.

| Attribute | Value |
|---|---|
| Repository | https://github.com/bluenviron/mediamtx |
| License | MIT (no trademark grant; see `TRADEMARK.md`) |
| Stars | ~19.6k |
| Binary | Single static Go executable; no interpreter, no FFmpeg dependency |
| Platforms | Linux, Windows, macOS |
| Concurrency model | Single process, many goroutines, channel-based shared state |
| Default ports | RTSP `:8554`, RTMP `:1935`, SRT `:8890`, HLS `:8888`, WebRTC `:8889`, API `:9997`, Metrics `:9998`, Playback `:9996` |

## Core Abstractions

### Path

A **Path** is the central domain object — it represents one named stream (e.g.
`/cam1`) and owns its lifecycle states:

- **Empty** — no publisher, no reader
- **Ready** — at least one publisher connected
- **ReadyOnDemand** — no publisher yet, but a reader arrived and triggered
  `runOnDemand` (e.g. start pulling from an RTSP source)
- **NotReady** — publisher disconnected, but readers still attached; the
  stream may serve a frozen frame or attempt auto-reconnect

Each Path contains:

- 1 **Stream** (provided by one publisher or one external source)
- N **Readers** (subscribers via any supported protocol)
- 0..1 **Recorder** (writes fMP4 or MPEG-TS to disk if configured)
- 0..N **Hook listeners** (fork-and-exec external commands on lifecycle events)

### Stream

A **Stream** is the unit of data flowing through the system. It owns:

- Video tracks (H264, H265, AV1, VP8, VP9, MJPEG, MPEG-2, MPEG-4)
- Audio tracks (AAC, Opus, MP3, G.711 family, G.722, G.723, G.726, G.729, LPCM)
- Metadata tracks (KLV for drone / surveillance telemetry)

The Stream object does **not** decode frames — it carries packets (NAL units
for video, samples for audio) between publishers and readers. This is what
makes "no re-encoding, zero pixel processing" possible, and why MediaMTX can
fan out one publisher to many readers without burning GPU.

## Architecture Diagrams

### 1. High-level layered architecture

```mermaid
graph TB
    subgraph EXT["External"]
        CAM["RTSP Camera<br/>(Hikvision / Dahua)"]
        BROWSER["WebRTC Browser"]
        HLS_PLAYER["HLS Player"]
    end

    subgraph MTX["MediaMTX process — single Go binary"]
        subgraph SRV["servers/ — one goroutine per port"]
            RTSP["RTSP :8554"]
            WEBRTC["WebRTC :8889"]
            HLS["HLS :8888"]
            RTMP["RTMP :1935"]
            SRT["SRT :8890"]
            API["Control API :9997"]
            METRICS["Prometheus :9998"]
            PLAYBACK["Playback :9996"]
        end

        subgraph PROT["protocols/ — wire protocol impls"]
            PRTSP["rtsp"]
            PWEBRTC["webrtc"]
            PHLLS["hls"]
            PRTMP["rtmp"]
            PSRT["srt"]
            PMOQ["moq"]
        end

        subgraph CORE["core/ — protocol-agnostic"]
            PM["Path Manager<br/>(auth + lifecycle)"]
            PATH["Path<br/>(state machine)"]
            STRM["Stream<br/>(video + audio tracks)"]
        end

        subgraph SIDE["Sidecars"]
            REC["recorder/<br/>→ fMP4 / MPEG-TS"]
            HOOKS["hooks/<br/>+ externalcmd/<br/>→ fork+exec"]
            AUTH["auth/<br/>internal / http / jwt"]
            CONF["conf/ + confwatcher/<br/>YAML + hot reload"]
        end
    end

    CAM -->|RTSP publish| RTSP
    BROWSER -->|WebRTC read| WEBRTC
    HLS_PLAYER -->|HLS read| HLS

    RTSP --> PRTSP
    WEBRTC --> PWEBRTC
    HLS --> PHLLS
    RTMP --> PRTMP
    SRT --> PSRT

    PRTSP --> PATH
    PWEBRTC --> PATH
    PHLLS --> PATH
    PRTMP --> PATH
    PSRT --> PATH

    PM --> PATH
    PATH --> STRM
    PATH --> REC
    PATH --> HOOKS
    PM --> AUTH
    PM --> CONF
    API --> PM
    METRICS --> PATH
```

**Observation**: there is exactly one `core/` layer. Protocols sit *above* it
(they speak wire formats) and servers sit *above* protocols (they bind ports
and spawn goroutines). Nothing in `core/` knows about RTSP, WebRTC, or HLS —
this is what makes the auto-protocol-conversion feature trivial.

### 2. Stream data flow — RTSP in, WebRTC out

```mermaid
sequenceDiagram
    autonumber
    participant Cam as Camera (RTSP)
    participant RS as RTSP Server :8554
    participant Pub as Publisher goroutine
    participant Stm as Stream (in-memory channel)
    participant Rd as Reader goroutine (WebRTC)
    participant Br as Browser

    Cam->>RS: ANNOUNCE / RECORD (RTP packets)
    RS->>Pub: spawn publisher goroutine

    loop every NAL unit
        Cam->>RS: RTP packet (H264)
        RS->>Pub: read from socket
        Pub->>Stm: write unit (zero-copy)
    end

    Br->>WS: HTTP/3 signaling (offer)
    WS->>Rd: spawn reader goroutine

    loop every NAL unit
        Rd->>Stm: read unit
        Rd->>Br: RTP/QUIC packet
    end

    Note over Stm: in-memory ring buffer<br/>shared by N readers<br/>single publisher
    Note over Pub,Rd: no re-encoding<br/>(zero pixel processing)
```

**Latency budget for the in-process portion** (Pub → Stm → Rd): well under 1 ms.
End-to-end latency is dominated by the slowest protocol on either edge:
RTSP-over-TCP ~50 ms, WebRTC/QUIC ~100–200 ms, HLS ~2–10 s (intentional).

### 3. Path lifecycle state machine

```mermaid
stateDiagram-v2
    [*] --> Empty: path created
    Empty --> Ready: first publisher connects
    Empty --> ReadyOnDemand: first reader connects<br/>triggers runOnDemand
    ReadyOnDemand --> Ready: source pulled<br/>or publisher arrives
    Ready --> NotReady: publisher disconnected
    NotReady --> Ready: publisher reconnects
    NotReady --> Empty: readers gone + retention
    Ready --> Empty: no publishers + no readers
    ReadyOnDemand --> Empty: no readers
    Ready --> Ready: additional readers/publishers join
```

### 4. Internal package dependency graph

```mermaid
graph LR
    subgraph leaf["leaf packages (no internal deps)"]
        defs["defs/<br/>Stream, Track, Codec types"]
        logger["logger/<br/>structured"]
        rlimit["rlimit/<br/>fd limits"]
        certloader["certloader"]
        ntpestimator["ntpestimator"]
    end

    subgraph util["utilities"]
        conf["conf/<br/>YAML parse"]
        confwatcher["confwatcher/<br/>fsnotify"]
        auth["auth/<br/>internal/http/jwt"]
        metrics["metrics/<br/>Prometheus"]
        externalcmd["externalcmd/<br/>fork+exec"]
        recorder["recorder/<br/>fMP4/MPEG-TS"]
        recordcleaner["recordcleaner"]
        recordstore["recordstore"]
        stream["stream/<br/>Stream type"]
    end

    subgraph core["core/ (orchestration)"]
        path["path.go"]
        pathmanager["path_manager.go"]
    end

    subgraph prot["protocols/ (wire format)"]
        rtsp["rtsp"]
        rtmp["rtmp"]
        srt["srt"]
        webrtc["webrtc"]
        hls["hls"]
        moq["moq"]
        mpegts["mpegts"]
        rtp["rtp"]
        rpicam["rpicameras"]
        webcams["webcams"]
    end

    subgraph servers["servers/ (port bindings)"]
        srtsp["rtsp :8554"]
        srtmp["rtmp :1935"]
        ssrt["srt :8890"]
        swebrtc["webrtc :8889"]
        shls["hls :8888"]
        sapi["api :9997"]
        smetrics["metrics :9998"]
        splayback["playback :9996"]
        spprof["pprof"]
    end

    conf --> defs
    auth --> conf
    recorder --> recordstore
    recorder --> recordcleaner
    recorder --> defs
    stream --> defs

    rtsp --> stream
    rtsp --> defs
    rtmp --> stream
    srt --> stream
    webrtc --> stream
    hls --> stream
    moq --> stream
    mpegts --> stream
    rtp --> stream
    rpicam --> stream
    webcams --> stream

    path --> stream
    path --> defs
    pathmanager --> path
    pathmanager --> auth
    pathmanager --> conf
    pathmanager --> hooks["hooks/"]
    pathmanager --> recorder

    srtsp --> rtsp
    srtmp --> rtmp
    ssrt --> srt
    swebrtc --> webrtc
    shls --> hls
    sapi --> pathmanager
    sapi --> api["api/"]
    smetrics --> metrics
    splayback --> recordstore
    splayback --> pathmanager
```

## Goroutine & Concurrency Model

MediaMTX is **one Go process, many goroutines, channel-based coordination**:

- **One goroutine per protocol server** listening on its port (accept loop).
- **One goroutine per accepted connection** — a publisher goroutine or a
  reader goroutine, depending on the protocol handshake.
- **The `Stream` object is the synchronization point** between publishers
  and readers. It exposes a small ring buffer of media units and a fan-out
  channel.
- **No shared mutable state across goroutines** — coordination via channels
  and `context.Context` for cancellation. Graceful shutdown propagates a
  single root `context` through the tree.
- **No re-encoding** = no GPU contention, no frame copies; bandwidth to
  readers is the only scaling limit.

## Protocol Matrix

| Protocol | Publish (camera → MTX) | Read (MTX → client) | Notes |
|---|---|---|---|
| RTSP | ✅ | ✅ | Default camera protocol; TCP and UDP transports |
| RTMP | ✅ | ✅ | Legacy cameras, OBS |
| SRT | ✅ | ✅ | UDP-based, low-latency, handles packet loss |
| WebRTC | ✅ | ✅ | Browser-grade latency; needs STUN/TURN for NAT |
| HLS | ✅ (LL-HLS) | ✅ | High latency (segment-based); CDN-friendly |
| MPEG-TS | ✅ | ❌ | UDP unicast/multicast input only |
| RTP | ✅ | ❌ | Bare RTP ingest |
| Media-over-QUIC | ✅ | ✅ | Experimental; QUIC-based streaming |
| MJPEG | ✅ (input only) | ❌ | Cameras can publish MJPEG; no MJPEG **read** endpoint |
| Raspberry Pi Camera | ✅ (native source) | — | Direct V4L2 capture |
| Generic USB webcam | ✅ (native source) | — | Direct V4L2 capture |

**Note**: MediaMTX does **not** expose a MJPEG read endpoint. The original
`rtsp-simple-server` did, but MediaMTX dropped it. Consumers that want
low-overhead per-frame access should use RTSP (`rtsp://mediamtx:8554/{cam}`),
which decodes via FFmpeg or GStreamer just as efficiently as MJPEG.

---

# Part 2 — Configuration & Operations

## Configuration

`mediamtx.yml` is the single config file (~850-line default with all options
commented). Notable sections:

```yaml
logLevel: info
logDestinations: [stdout]
api: yes                # Control API on :9997
metrics: yes            # Prometheus metrics on :9998
pprof: no
playback: yes
authMethod: internal    # or http / jwt

# Recording defaults — applied to every path unless overridden.
# Recording itself is configured PER PATH (see `paths:` below).
pathDefaults:
  record: false
  recordPath: ./recordings/%path/%Y-%m-%d_%H-%M-%S-%f
  recordFormat: fmp4            # or mpegts
  recordPartDuration: 1s       # fMP4 part size (== RPO)
  recordSegmentDuration: 1h    # how often a new segment file is created
  recordMaxPartSize: 50M       # safety cap on individual parts
  recordDeleteAfter: 168h      # retention (set 0s to disable)

paths:
  cam1:
    # Source (pulls from upstream camera)
    source: rtsp://camera-ip/stream1
    sourceOnDemand: yes
    sourceOnDemandStartTimeout: 10s

    # Recording (overrides pathDefaults)
    record: true
    recordPath: /recordings/cam1/%Y-%m-%d_%H-%M-%S-%f
    recordFormat: fmp4
    recordSegmentDuration: 1h
    recordDeleteAfter: 168h

    # Hooks (correct names — see Hooks section below)
    runOnInit: /opt/scripts/on-init.sh
    runOnDemand: python3 /opt/scripts/lazy-load.py
    runOnAvailable: /opt/scripts/notify-ready.sh
    runOnAvailableRestart: false
    runOnUnavailable: /opt/scripts/notify-down.sh
    runOnRead: /opt/scripts/audit-watch.sh
    runOnUnread: /opt/scripts/audit-unwatch.sh

  # regex path matching — match any path
  "~^(.+)$":
    source: rtsp://upstream-server/$G1
    sourceOnDemand: yes
```

Key behaviors:

- **Hot reload** via fsnotify (`internal/confwatcher/`) — `mediamtx.yml` is
  reparsed on save; existing connections are **not** dropped.
- **Environment variables** can override any setting (e.g.
  `MTX_PATHS_CAM1_SOURCE=rtsp://...`).
- **Path name regexes** (`~^(.+)$`) let you forward arbitrary paths to an
  upstream MediaMTX (the "read replica" pattern).

> **Note**: There is **no global `record:` section** in MediaMTX. Recording
> is per-path (or per `pathDefaults:`). The fields are `record`, `recordPath`,
> `recordFormat`, `recordPartDuration`, `recordSegmentDuration`,
> `recordMaxPartSize`, `recordDeleteAfter`. Older docs and tutorials that
> show `segmentDuration` or `retention` at the top level are incorrect.

## Authentication

Three modes, selectable via `authMethod`:

| Mode | Use case | Tradeoff |
|---|---|---|
| **internal** | Self-contained, no external deps | Plain YAML file; passwords can be hashed with Argon2 or SHA256 |
| **http** | Delegate to existing auth service | One HTTP POST per auth attempt; can be slow at high QPS |
| **jwt** | Federated identity (Keycloak, Auth0, custom) | One JWKS fetch, then offline verification; fastest |

**Permissions** are 6 actions × per-path regex:

| Action | Meaning |
|---|---|
| `publish` | Push a stream to a path |
| `read` | Subscribe to a path |
| `playback` | Read from recording playback server |
| `api` | Call Control API endpoints |
| `metrics` | Scrape Prometheus metrics |
| `pprof` | Access Go profiling endpoints |

For JWT mode the token must include a `mediamtx_permissions` claim matching
the permission schema.

## Hooks

Hooks are **fire-and-forget external commands** that run on lifecycle events.
Configured per-path (verified against MediaMTX v1.19.3 configuration
reference):

```yaml
paths:
  cam1:
    runOnInit: /opt/scripts/on-init.sh                      # path created
    runOnDemand: python3 /opt/scripts/lazy-load.py          # first reader, no publisher
    runOnDemandRestart: false
    runOnDemandCloseAfter: 10s
    runOnUnDemand: /opt/scripts/on-undemand.sh              # no readers anymore
    runOnAvailable: /opt/scripts/on-available.sh            # stream ready
    runOnAvailableRestart: false
    runOnUnavailable: /opt/scripts/on-unavailable.sh        # stream gone
    runOnOnline: /opt/scripts/on-online.sh                  # publisher connected
    runOnOnlineRestart: false
    runOnOffline: /opt/scripts/on-offline.sh                # publisher disconnected
    runOnRead: /opt/scripts/reader-joined.sh
    runOnUnread: /opt/scripts/reader-left.sh
    runOnRecordSegmentCreate: /opt/scripts/on-rec-start.sh
    runOnRecordSegmentComplete: /opt/scripts/on-rec-end.sh
```

### Hook semantics

| Hook | Trigger | Termination |
|---|---|---|
| `runOnInit` | Path created (server start) | Program shutdown (SIGINT) |
| `runOnDemand` | First reader, no publisher yet | No readers anymore (after `runOnDemandCloseAfter` delay) |
| `runOnUnDemand` | No readers anymore | Process exit |
| `runOnAvailable` | Stream can be read | Stream not available anymore (SIGINT) |
| `runOnUnavailable` | Stream not available anymore | Process exit |
| `runOnOnline` | Stream from an online source (not offline segment) | Stream goes offline |
| `runOnOffline` | Stream goes offline | Process exit |
| `runOnRead` | Client starts reading | Client stops reading (SIGINT) |
| `runOnUnread` | Client stops reading | Process exit |
| `runOnRecordSegmentCreate` | New recording segment file created | Process exit |
| `runOnRecordSegmentComplete` | Recording segment finished | Process exit |

### Environment variables passed to hooks

Different hooks get different env vars (per official docs):

- **All hooks**: `MTX_PATH`, `RTSP_PORT`, and `G1, G2, ...` for regex paths
- **On-demand hooks** (`runOnDemand`, `runOnUnDemand`): `MTX_QUERY` (url-encoded)
- **Availability hooks** (`runOnAvailable`, `runOnUnavailable`,
  `runOnOnline`, `runOnOffline`): `MTX_QUERY`, `MTX_SOURCE_TYPE`,
  `MTX_SOURCE_ID`
- **Read hooks** (`runOnRead`, `runOnUnread`): `MTX_QUERY`,
  `MTX_READER_TYPE`, `MTX_READER_ID`

**There is no `MTX_READER_IP`** — IP is logged but not passed via env
vars. Older tutorials mentioning it are incorrect.

### Process lifecycle

- Each hook is a `fork+exec` child process spawned by
  `internal/externalcmd/`.
- Long-running hooks (Available, Online, Demand, Read) receive **SIGINT**
  when their trigger condition ends — they are expected to exit
  gracefully on SIGINT (FFmpeg handles this correctly; custom scripts need
  to trap SIGINT).
- Short-lived hooks (Init, UnDemand, Unavailable, Offline, Unread, Record*)
  just run and exit.
- Exit code 0 = success; non-zero is logged as a warning.
- **Fire-and-forget** — child process cannot block the main event loop, but
  a hung child will leak an OS process.
- `*Restart: yes` makes MediaMTX auto-restart the command if it exits
  unexpectedly. For long-running hooks, the command is expected to run
  indefinitely until SIGINT.

Hooks are the **primary integration point** for sidecars. The 6 sidecar
patterns below are all variations of hooks + RTSP/HTTP.

## Sidecar Integration Patterns

MediaMTX exposes **6 distinct patterns** for integrating external sidecars.
These are not mutually exclusive — production deployments typically combine
several.

### Pattern 1 — Sidecar publishes directly

Sidecar is an ordinary client that pushes its stream into MediaMTX:

```bash
# Sidecar A — re-encoded stream from FFmpeg
ffmpeg -i raw.mp4 -c:v libx264 -f rtsp rtsp://mediamtx:8554/cam1

# Sidecar B — synthetic / AI-generated stream
python3 -m virex.synthetic_publisher \
  --output rtsp://mediamtx:8554/cam2
```

MediaMTX treats the sidecar identically to a camera — same auth, same
protocol handling, same lifecycle.

### Pattern 2 — MediaMTX pulls from sidecar

Reverse direction — MediaMTX pulls from the sidecar's RTSP server:

```yaml
paths:
  ai_output:
    source: rtsp://ai-sidecar:8554/inference
    sourceOnDemand: yes
    sourceMaxReconnectTime: 10s
```

MediaMTX handles reconnection logic; the sidecar just exposes an RTSP
endpoint and forgets about it.

### Pattern 3 — `runOnDemand` (lazy spawn)

First reader triggers sidecar spawn; MediaMTX sends **SIGINT** to the
sidecar when the last reader disconnects (after `runOnDemandCloseAfter`
delay, default 10 s):

```yaml
paths:
  cam1_annotated:
    runOnDemand: >
      python3 -m ai_backend.publisher
        --camera cam1
        --output rtsp://localhost:$RTSP_PORT/$MTX_PATH
    runOnDemandRestart: true
    runOnDemandStartTimeout: 10s
    runOnDemandCloseAfter: 10s
```

Useful when the AI model takes 10+ seconds to load and shouldn't stay
loaded when nobody's watching. The sidecar script must trap SIGINT and
exit gracefully (FFmpeg and most well-written tools do this by default).

### Pattern 4 — `runOnAvailable` + `runOnUnavailable` (lifecycle-bound)

Sidecar spawns when the stream becomes available, is sent SIGINT when it
becomes unavailable:

```yaml
paths:
  cam1:
    runOnAvailable: python3 -m ai_backend.listener --camera $MTX_PATH
    runOnUnavailable: kill $(pgrep -f "ai_backend.listener.*$MTX_PATH") || true
```

> **Naming note**: There is no `runOnReady` / `runOnNotReady` in
> MediaMTX v1.19.3. The correct equivalents are `runOnAvailable` /
> `runOnUnavailable` (stream is readable) and `runOnOnline` /
> `runOnOffline` (publisher connected). Older tutorials and docs that
> show `runOnReady` are outdated.

For AI that must process every frame continuously (event detection,
logging). This is also the pattern used to spawn FFmpeg transcode sidecars
(see [Unified-Format Pipeline](#unified-format-pipeline-with-transcode-sidecars)).

### Pattern 5 — Proxy (chain MediaMTX instances)

MediaMTX ↔ MediaMTX through RTSP. Used for read replicas, cross-region
relay, A/B deployment:

```yaml
# Read replica config
paths:
  "~^(.+)$":
    source: rtsp://origin-mediainstance:8554/$G1
    sourceOnDemand: yes
```

### Pattern 6 — Recording consumer (read-only sidecar)

Sidecar watches the recording directory produced by MediaMTX:

```yaml
record:
  path: /recordings/%path/%Y-%m-%d_%H-%M-%S.mp4
  format: fmp4
```

Used for offline batch inference, cloud archival, backup.

### Pattern selection decision tree

```
Q: Is the sidecar a stream source or consumer?
├── Source (sidecar produces the stream)
│   ├── Always-on (24/7)        → Pattern 4 (runOnAvailable)
│   ├── Lazy / on first viewer  → Pattern 3 (runOnDemand)
│   └── Stateless / MTX handles reconnect → Pattern 2 (source:)
│
├── Consumer (sidecar reads stream to do work)
│   ├── Continuous AI / logging → Pattern 4 hook + sidecar RTSP-subscribes
│   ├── Publishes a new annotated/derived stream → Pattern 1
│   └── Reading recordings → Pattern 6 (file watch)
│
└── MediaMTX itself is the sidecar
    └── Pattern 5 (proxy / read replica)
```

## Built-in Recording

Recording is configured **per path** (or under `pathDefaults:` for all
paths). The actual MediaMTX 1.19.3 fields are:

```yaml
pathDefaults:              # or under each path in `paths:`
  record: true
  recordPath: ./recordings/%path/%Y-%m-%d_%H-%M-%S-%f
  recordFormat: fmp4        # or mpegts
  recordPartDuration: 1s   # fMP4 part size — also the recovery point objective
  recordSegmentDuration: 1h # how often a new segment file is closed
  recordMaxPartSize: 50M   # safety cap on individual part size
  recordDeleteAfter: 168h  # retention period; 0s disables auto-deletion
```

**Field semantics**:

- `recordPartDuration` — for fMP4, each segment is concatenation of parts
  of this duration; for MPEG-TS, packet flush period. On crash, last part
  is lost → this is the RPO.
- `recordSegmentDuration` — how often a new segment file is created (e.g.
  hourly rotation). Independent from part duration.
- `recordDeleteAfter` — segments older than this are auto-deleted.

**Common mistakes** (avoid these — seen in older tutorials):
- `segmentDuration` ❌ — actual field is `recordSegmentDuration`
- `retention` ❌ — actual field is `recordDeleteAfter`
- Top-level `record:` section ❌ — recording is per-path or in
  `pathDefaults:`, not a separate global block

- **fMP4** is recommended (CMAF-friendly, works with most players).
- **MPEG-TS** is legacy.
- Auto-deletion uses the `recordDeleteAfter` field; no separate
  `recordcleaner` daemon (the cleanup runs in-process).
- **Playback server** (`:9996`) serves recordings via a custom protocol.

Recording happens at the **path level**, so in a dual-layer architecture (see
[Unified-Format Pipeline](#unified-format-pipeline-with-transcode-sidecars)),
configure recording on the normalized layer to ensure a single uniform codec
on disk.

## Control API

When `api: yes`, listens on `:9997` (localhost by default — see auth section
to expose). OpenAPI spec is in `api/openapi.yaml`. Useful endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v3/paths/list` | List all active paths |
| `GET` | `/v3/paths/{name}` | Get path details (readers, bitrate, etc.) |
| `POST` | `/v3/paths/{name}/kick` | Disconnect all readers of a path |
| `GET` | `/v3/config/paths/get/{name}` | Read runtime config for a path |

## Metrics

Prometheus exposition on `:9998` (localhost by default). Key metrics:

| Metric | Type | Description |
|---|---|---|
| `mediamtx_bytes_received` | counter | Cumulative bytes received per path |
| `mediamtx_bytes_sent` | counter | Cumulative bytes sent per path |
| `mediamtx_readers` | gauge | Current reader count per path |
| `mediamtx_streams` | gauge | Currently active paths |
| `mediamtx_rtsp_sessions_*` | various | Per-RTSP-session latency, errors |

These complement sidecar-side metrics (e.g. FFmpeg progress, AI inference
latency) for full-stack observability.

## Scalability Patterns

MediaMTX is **single-process** by design. The bottleneck for a single
instance is almost always **egress bandwidth**, not CPU. For larger
deployments:

### Pattern A — Read replicas

Deploy one **origin** MediaMTX that accepts publishers, plus N **read
replicas** that pull from origin and serve readers. A load balancer
distributes readers.

```yaml
# read replica config
webrtcLocalUDPAddress:
webrtcICEServers2:
  - url: stun:stun.l.google.com:19302

paths:
  "~^(.+)$":
    source: rtsp://origin-host:8554/$G1
    sourceOnDemand: yes
```

- Layer-4 LB for RTSP / RTMP / SRT (single TCP/UDP connection per reader)
- Layer-7 LB with sticky sessions for HLS / WebRTC (multi-request session)

### Pattern B — CDN in front of HLS

For massive scale (10k+ concurrent viewers) put a CDN in front of MediaMTX's
HLS endpoint. Requires:

- Setting `hlsVariant: fmp4` (segment-based, cacheable)
- Setting `hlsCDNSecret` and having the CDN inject `Authorization: Bearer`
  headers so MediaMTX can authenticate CDN requests
- Disabling LL-HLS playlists (not cacheable)

**Limitations**: HLS-only (no WebRTC); standard MediaMTX auth bypassed.

---

# Part 3 — Codec Compatibility & Transcoding

## Why MediaMTX Doesn't Transcode

**MediaMTX is a codec-agnostic passthrough router.** Whatever codec the
publisher sends (H.264, H.265, AV1, MJPEG, AAC, Opus) is routed to readers
unchanged. This is intentional — no re-encoding means zero pixel
processing, ~1 ms in-process latency, and almost no CPU.

The official docs state:

> To change the format, codec or compression of a stream, use **FFmpeg** or
> **GStreamer** together with MediaMTX.

The MediaMTX GitHub issues confirm this is the maintainer's standing
position: every "add transcoding" feature request is closed with "use an
external FFmpeg/GStreamer hook" (issues #5405, #5327, #4669, #3962, #4290,
#5887, etc.).

### When this bites you

When a reader (browser, AI consumer, recorder) connects to MediaMTX, the
RTSP handshake negotiates the codec via SDP exchange. If the reader
cannot decode the publisher's codec, the SDP negotiation or the SETUP
call typically fails and the client never establishes a session.

(Note: RTSP status `461 UnsupportedTransport` is specifically for
transport mismatch — TCP vs UDP — not codec mismatch. Codec mismatch
typically surfaces as a connection-level failure rather than a specific
status code.)

Common failure scenarios in surveillance:

- Hikvision camera publishes H.265 main stream → Safari browser (Intel
  Mac) tries to subscribe → connection fails
- Old IP cam publishes MJPEG only → AI backend using PyAV with H.264
  decoder fails
- Camera codec bitstream has non-standard extensions → generic decoder
  fails

### Why not patch MediaMTX to transcode internally?

| Self-rolled transcoding in MediaMTX | External FFmpeg sidecar |
|---|---|
| ❌ Fork maintenance nightmare | ✅ FFmpeg is industry standard, actively updated |
| ❌ Massive codec logic rewrite | ✅ FFmpeg handles every edge case already |
| ❌ Locked-in upstream updates | ✅ MediaMTX keeps normal release cadence |
| ❌ Loses 19.6k stars of community | ✅ Upstream compatibility preserved |
| ❌ License entanglement | ✅ FFmpeg's LGPL/GPL doesn't change anything |

**Bottom line:** always externalize transcoding.

## Codec Compatibility Matrix

The intersection of all real-world consumers is **H.264**. If you need a
stream that works everywhere — including AI inference — transcode to H.264
High Profile.

| Consumer | H.264 | H.265/HEVC | VP9 | AV1 | MJPEG |
|---|---|---|---|---|---|
| Safari (macOS) | ✅ all | ⚠️ Apple Silicon only | ⚠️ partial 2018+ | ❌ partial recent | ❌ |
| Safari (iOS) | ✅ | ⚠️ A11 chip+ | ⚠️ iOS 14+ | ❌ | ❌ |
| Chrome / Edge / Firefox | ✅ | ✅ | ✅ | ✅ | ❌ |
| WebRTC (all browsers) | ✅ | ❌ mostly | ✅ Chrome/Firefox | ⚠️ Chrome only | ❌ |
| PyAV / FFmpeg | ✅ | ✅ (build-dep) | ✅ | ✅ | ✅ |
| ONNX Runtime | depends on PyAV | depends on PyAV | depends | depends | depends |
| MediaMTX recorder (fMP4) | ✅ | ✅ | ✅ | ✅ | ✅ |

## Universal Transcoding Format Spec

For a stream that works across **Safari + AI object detection + depth
estimation + WebRTC browsers + MediaMTX recording + third-party NVR/VMS**,
the lowest-common-denominator format is:

```
Codec:       H.264 (libx264 software or h264_nvenc hardware)
Profile:     High
Level:       4.2  (supports 1080p @ 30 fps)
Pixel fmt:   yuv420p  (8-bit, 4:2:0)
Rate ctrl:   CBR (constant bit rate) — mandatory for live
Bitrate:     4 Mbps target, 4 Mbps max, 2 Mbps buffer
GOP:         50 frames @ 25 fps (= 2 seconds keyframe interval)
B-frames:    0 (disabled for zero buffering delay)
Preset:      x264 ultrafast OR NVENC p4
Tune:        x264 zerolatency OR NVENC ll
Resolution:  1920x1080 (or 640x360 for AI-only stream)
Frame rate:  25 fps (PAL) or 30 fps (NTSC)
Audio:       AAC, 128 kbps, 48 kHz, stereo
Container:   RTSP output, fMP4 for recording
Latency add: ~200–500 ms from transcode pipeline
```

### Why H.264 High Profile + yuv420p

- **H.264** = the only codec every consumer on the list decodes natively
- **High Profile** = ~10–15 % better compression than Main Profile at the
  same quality, with no compatibility loss (all browsers / ONNX / WebRTC
  accept it)
- **yuv420p** = the only 8-bit pixel format every web video stack accepts
  (yuv422p, yuv444p, nv12 are not browser-compatible)

### Why CBR (not VBR)

- VBR looks better at the same average bitrate but causes **jitter** in
  live delivery — bandwidth spikes when scene is complex, and the
  consumer's buffer either underflows (frame drops) or overflows
  (latency increases)
- CBR is what Twitch, YouTube Live, OBS, WebRTC SFU, and WebRTC
  standards all require
- For AI inference, CBR bitrate stability also means predictable
  per-frame decode time

### Why no B-frames

- B-frames require forward reference decoding → encoder must hold
  multiple frames in buffer → +100–500 ms latency
- For real-time AI and live monitoring, B-frames are pure overhead
- Industry default for live: `bf=0`

### Per-consumer compatibility (H.264 High + yuv420p)

| Consumer | Accepts? |
|---|---|
| Safari (macOS + iOS) | ✅ |
| Chrome / Edge / Firefox | ✅ |
| WebRTC browser stack | ✅ |
| Virex ai-backend (PyAV + ONNX) | ✅ |
| Depth Anything V2 / YOLO-depth | ✅ (via PyAV decode to BGR) |
| MediaMTX recording (fMP4) | ✅ |
| VLC / OBS / ffmpeg replay | ✅ |
| Hikvision / Dahua NVR ingest | ✅ |

## Unified-Format Pipeline with Transcode Sidecars

The canonical real-world deployment pattern: **heterogeneous cameras →
unified H.264 format → single recording format → single AI format**.

### Architecture diagram

```mermaid
flowchart TB
    subgraph CAMS["📷 Cameras (heterogeneous codecs)"]
        A1[Hikvision<br/>H.265 RTSP]
        A2[Dahua<br/>H.264 RTSP]
        A3[Old IP cam<br/>MJPEG RTSP]
        A4[USB webcam<br/>via V4L2]
    end

    subgraph L1["📦 MediaMTX Layer 1 (raw ingest)"]
        P1["cam1_raw<br/>(H.265)"]
        P2["cam2_raw<br/>(H.264)"]
        P3["cam3_raw<br/>(MJPEG)"]
        P4["cam4_raw<br/>(V4L2)"]
        CFG["mediamtx.yml<br/>runOnAvailable hook<br/>spawns transcoder"]
    end

    subgraph TX["🔄 Transcode sidecars (1 per camera)"]
        T1["ffmpeg<br/>H.265 → H.264<br/>NVENC or libx264"]
        T2["ffmpeg<br/>H.264 → H.264<br/>(re-encode for uniform GOP)"]
        T3["ffmpeg<br/>MJPEG → H.264"]
        T4["gstreamer<br/>V4L2 → H.264"]
    end

    subgraph L2["📦 MediaMTX Layer 2 (normalized)"]
        NP1["cam1_normalized<br/>(H.264 High, CBR)"]
        NP2["cam2_normalized"]
        NP3["cam3_normalized"]
        NP4["cam4_normalized"]
        REC["🗄️ Recording<br/>fMP4 H.264 High<br/>uniform segments"]
    end

    subgraph CON["👥 Consumers (all see same format)"]
        BR["Browser WebRTC<br/>H.264 + yuv420p"]
        IOS["iOS / Android HLS"]
        AI["Virex ai-backend<br/>PyAV → ONNX"]
        AUD["Audit log"]
    end

    A1 -->|H.265 RTSP| P1
    A2 -->|H.264 RTSP| P2
    A3 -->|MJPEG RTSP| P3
    A4 -->|V4L2| P4

    CFG -.->|spawns on ready| T1
    CFG -.->|spawns| T2
    CFG -.->|spawns| T3
    CFG -.->|spawns| T4

    P1 -->|RTSP pull| T1
    P2 -->|RTSP pull| T2
    P3 -->|RTSP pull| T3
    P4 -->|RTSP pull| T4

    T1 -->|H.264 RTSP| NP1
    T2 -->|H.264 RTSP| NP2
    T3 -->|H.264 RTSP| NP3
    T4 -->|H.264 RTSP| NP4

    NP1 --> REC
    NP2 --> REC
    NP3 --> REC
    NP4 --> REC

    NP1 --> BR
    NP1 --> IOS
    NP1 --> AI
    NP1 --> AUD
```

### Why two layers (not one)

| Design | Pros | Cons |
|---|---|---|
| **Single layer**: camera → MediaMTX → fanout | Simple, fewer processes | Every consumer must handle every codec |
| **Two layers** (raw + normalized) | All consumers see unified H.264; recording is uniform; AI stack stays simple | +1 process per camera, ~200–500 ms transcode latency |

The dual-layer pattern centralizes **codec heterogeneity** in one place
(the transcode sidecar) so every other component — recording, browser, AI,
audit — sees the same format.

### `mediamtx.yml` skeleton for dual-layer

```yaml
logLevel: info
api: yes
metrics: yes

paths:
  # ============ Layer 1: RAW (camera ingest + hook spawns transcoder) ============
  cam1_raw:
    runOnAvailable: >
      ffmpeg -hwaccel cuda -hwaccel_output_format cuda -extra_hw_frames 8
        -fflags +genpts -rtsp_transport tcp
        -i rtsp://localhost:$RTSP_PORT/cam1_raw
        -c:v h264_nvenc -profile:v high -level:v 4.2
        -pix_fmt yuv420p -preset p4 -tune ll
        -rc cbr -b:v 4M -maxrate 4M -bufsize 2M
        -g 50 -keyint_min 50 -sc_threshold 0 -bf 0
        -r 25 -vf "scale_npp=1920:1080:interp_algo=super"
        -c:a aac -b:a 128k -ar 48000 -ac 2
        -f rtsp -muxdelay 0 -muxpreload 0
        rtsp://localhost:$RTSP_PORT/cam1_normalized
    runOnAvailableRestart: true
    runOnUnavailable: pkill -f "ffmpeg.*cam1_raw" || true

  # ============ Layer 2: NORMALIZED (consumers + recording) ============
  cam1_normalized:
    record: true
    recordPath: /recordings/cam1/%Y-%m-%d_%H-%M-%S-%f
    recordFormat: fmp4
    recordSegmentDuration: 1h
    recordDeleteAfter: 168h
```

## Industry Best Practices

Verified findings from NVIDIA, Frigate, Twitch, and YouTube.

### NVIDIA FFmpeg transcoding guide (official, Jul 2019)

NVIDIA's developer blog lays out the GPU-accelerated pipeline:

- Use `-hwaccel cuda -hwaccel_output_format cuda` to **keep decoded frames
  in GPU memory** — avoids PCIe bus transfers that cap throughput
- Use `h264_nvenc` for H.264 hardware encoding (separate from CUDA cores;
  does not slow other GPU workloads)
- Use **`scale_npp` filter** for GPU-side resizing (not `scale` which is
  CPU)
- For **1:N transcoding** (one input → multiple resolutions), do one
  GPU resize and multiple encodes — saves PCIe bandwidth
- For multi-GPU systems, use `-hwaccel_device N` to assign work to a
  specific GPU

Reference: <https://developer.nvidia.com/blog/nvidia-ffmpeg-transcoding-guide/>

### Frigate production pattern (verified, docs.frigate.video)

Frigate's documented setup uses **two RTSP inputs per camera** with
different roles:

```yaml
cameras:
  back:
    ffmpeg:
      inputs:
        - path: rtsp://...:554/substream    # low-res sub-stream
          roles: [detect]                   # for object detection
        - path: rtsp://...:554/mainstream   # full-res main stream
          roles: [record]                   # for recording
```

**Why this matters**:
- IP cameras natively provide `main` (full-res) and `sub` (low-res) streams
- `sub` → AI detection (640x360 @ 5–10 fps = ~500 kbps, fits AI quality)
- `main` → recording (1080p @ 25 fps = full quality for archive)
- **No FFmpeg transcoding required** when the camera provides sub-stream
- Only transcode when camera has no sub-stream, or when sub-stream codec
  is incompatible

### Twitch / YouTube live encoding standards (industry de facto)

| Platform | Codec | Profile | Bitrate (1080p30) | Keyframe | Rate control |
|---|---|---|---|---|---|
| Twitch | H.264 | Baseline or Main | 6 Mbps | 2 s | CBR |
| YouTube Live | H.264 | High | 4–12 Mbps | 2 s | CBR |
| OBS default | H.264 | target-dependent | VBR/CBR | 2 s | both supported |
| WebRTC SFU | H.264 | Constrained Baseline | 1–4 Mbps | varies | CBR |

**Common conclusions**:
- 100% use H.264 (nobody uses H.265/AV1 as primary live format)
- **CBR** for live (VBR for VOD only)
- **Keyframe interval = 2 seconds** (= 50 frames @ 25 fps, 60 frames @ 30 fps)
- yuv420p universal

### Recommended FFmpeg command (industry best practice)

Combining NVIDIA + Twitch + Frigate findings. This is the canonical
transcode command to use with `runOnAvailable` hooks (also see the
`mediamtx.yml` example above):

```bash
ffmpeg -hwaccel cuda -hwaccel_output_format cuda -extra_hw_frames 8 \
  -fflags +genpts -rtsp_transport tcp \
  -i "$INPUT_RTSP_URL" \
  \
  -c:v h264_nvenc \
  -profile:v high \
  -level:v 4.2 \
  -pix_fmt yuv420p \
  -preset p4 \
  -tune ll \
  \
  -rc cbr \
  -b:v 4M \
  -maxrate 4M \
  -bufsize 2M \
  \
  -g 50 \
  -keyint_min 50 \
  -sc_threshold 0 \
  -bf 0 \
  \
  -r 25 \
  -vf "scale_npp=1920:1080:interp_algo=super" \
  \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  \
  -f rtsp -muxdelay 0 -muxpreload 0 \
  "$OUTPUT_RTSP_URL"
```

For systems without NVIDIA GPU, swap `h264_nvenc`/`scale_npp`/`preset p4`/`tune ll`
for `libx264`/`scale`/`preset ultrafast`/`tune zerolatency`. See the [CPU variant
below](#cpu-variant).

### CPU variant

When no NVIDIA GPU is available (e.g. RPi, Jetson without NVENC, budget
servers), use the CPU equivalent:

```bash
ffmpeg -fflags +genpts -rtsp_transport tcp \
  -i "$INPUT_RTSP_URL" \
  \
  -c:v libx264 \
  -profile:v high \
  -level:v 4.2 \
  -pix_fmt yuv420p \
  -preset ultrafast \
  -tune zerolatency \
  \
  -rc cbr \
  -b:v 4M \
  -maxrate 4M \
  -bufsize 2M \
  \
  -g 50 \
  -keyint_min 50 \
  -sc_threshold 0 \
  -bf 0 \
  \
  -r 25 \
  -vf "scale=1920:1080" \
  \
  -c:a aac \
  -b:a 128k \
  -ar 48000 \
  -ac 2 \
  \
  -f rtsp \
  -muxdelay 0 \
  -muxpreload 0 \
  "$OUTPUT_RTSP_URL"
```

### Per-parameter justification

| Parameter | Value | Why |
|---|---|---|
| `-c:v libx264` / `h264_nvenc` | H.264 | Universal consumer support |
| `-profile:v high` | High | Better compression than Main, no compat loss |
| `-level:v 4.2` | 4.2 | 1080p @ 30 fps headroom |
| `-pix_fmt yuv420p` | 4:2:0 8-bit | Browser-safe pixel format |
| `-preset ultrafast` / `p4` | fastest reasonable | Real-time requirement |
| `-tune zerolatency` / `ll` | zero-latency tune | Minimize encoder buffering |
| `-rc cbr` | constant bit rate | Live streaming stability |
| `-b:v 4M` | 4 Mbps target | AI quality + bandwidth budget |
| `-bufsize 2M` | 2 Mbps | Live streaming low buffer |
| `-g 50 -keyint_min 50` | 2-second GOP | Standard for seek-friendly live |
| `-sc_threshold 0` | fixed GOP | Predictable frame access for AI |
| `-bf 0` | no B-frames | Zero buffering delay |
| `-hwaccel cuda -hwaccel_output_format cuda` | GPU decode | NVIDIA recommendation — avoid PCIe copies |
| `-extra_hw_frames 8` | pre-allocated GPU buffers | Avoid jitter from dynamic allocation |
| `scale_npp` | GPU resize filter | NVIDIA recommendation — keep frames on GPU |
| `interp_algo=super` | super-sampling downscale | Best quality when downscaling |
| `-fflags +genpts` | regenerate PTS | Fixes bad camera timestamps (Hikvision) |
| `-rtsp_transport tcp` | TCP transport | Stable, no UDP packet loss |
| `-muxdelay 0 -muxpreload 0` | zero mux delay | Minimize pipeline latency |

### Dual-stream architecture (Frigate pattern adapted)

```mermaid
flowchart LR
    CAM[IP Camera<br/>main + sub stream]

    subgraph MTX["MediaMTX"]
        RAW[cam1_raw<br/>from camera]
        AI["cam1_ai<br/>640x360 H.264<br/>ultrafast, 2 Mbps"]
        ARC["cam1_archive<br/>1080p H.264<br/>slow preset, 6 Mbps"]
    end

    AI_PIPE[Virex ai-backend<br/>YOLO + Depth]
    ARC_STORE[Long-term storage]

    CAM -->|RTSP sub| RAW
    CAM -->|RTSP main| ARC

    RAW -->|FFmpeg transcode<br/>low-res H.264| AI
    RAW -.->|optional passthrough| ARC

    AI -->|MJPEG| AI_PIPE
    ARC --> ARC_STORE
```

**Per-stream parameters:**

| Stream | Resolution | Bitrate | Preset | Purpose |
|---|---|---|---|---|
| `cam1_ai` | 640x360 | 2 Mbps | ultrafast | YOLO detection, depth, segmentation |
| `cam1_archive` | 1920x1080 | 6 Mbps | slow / medium | Playback, evidence, audit |

**When to use each stream**:
- Camera has sub-stream → use sub-stream for AI (no FFmpeg needed for that
  path)
- Camera has no sub-stream → use FFmpeg to transcode a low-res variant
- Archive always uses main-stream passthrough (or one-time re-encode for
  storage optimization)

**Why dual stream**:
- **AI does not need full resolution** — YOLO at 640x640 input is standard
- **Bandwidth savings** — 640x360 @ 2 Mbps vs 1080p @ 6 Mbps = 3x reduction
  on the AI path
- **GPU savings** — YOLO inference scales with input pixels; smaller
  resolution = faster inference
- **Storage savings** — main stream can be archived without re-encoding
  (if camera codec already H.264) or with one higher-quality transcode

## Hikvision-Specific Notes

Hikvision cameras are by far the most common source of integration pain
because of their default codec and stream configuration. Specific points:

### RTSP URL conventions

Hikvision URL format depends on firmware generation:

```
# Modern firmware (most DS-2CD, DS-2DE since ~2018):
# Main stream (video only, H.264 or H.265)
rtsp://user:pass@camera-ip:554/Streaming/Channels/101

# Sub stream (typically H.264, lower resolution)
rtsp://user:pass@camera-ip:554/Streaming/Channels/102

# Third stream (some models)
rtsp://user:pass@camera-ip:554/Streaming/Channels/103

# Audio track (varies by firmware; check camera's "Video & Audio" settings)
rtsp://user:pass@camera-ip:554/Streaming/tracks/101
# or sometimes:
rtsp://user:pass@camera-ip:554/Streaming/Channels/101?audio=1
```

```
# Older firmware (<2018):
# Main stream
rtsp://user:pass@camera-ip:554/h264/ch1/main/av_stream

# Sub stream
rtsp://user:pass@camera-ip:554/h264/ch1/sub/av_stream
```

> **Channel numbering**: `<N><S>` where `N` is the camera number
> (1-based) and `S` is the stream index (`01` = main, `02` = sub,
> `03` = third). So `Channels/101` = camera 1 main stream.

Authentication: Hikvision requires **RTSP digest authentication** by
default (not Basic). FFmpeg and MediaMTX handle this transparently — but
you must include credentials in the URL.

### Default codec quirks

- **Hikvision default main stream is H.265** on most modern models
  (DS-2CD2xxx, DS-2DE4xxx, DS-2DF8xxx series). On older firmware and
  entry-level models (DS-2CD1xxx, DS-2CD3xxx Value series) it is H.264.
- **Hikvision sub stream is H.264** by default — this is the easy path
  for AI ingestion.
- **B-frame surprises**: Hikvision's default H.265 config sometimes
  enables B-frames, which can confuse AI decoders that expect
  decode-order = display-order. Symptom: artifacts in decoded frames.
  Fix: in camera settings, disable B-frames, or ensure the FFmpeg
  pipeline handles `reorder_queue_size` correctly.
- **Profile mismatch**: Hikvision's "Main" profile H.265 has occasional
  SEI (Supplemental Enhancement Information) messages that some
  decoders don't handle. If a Hikvision H.265 stream won't decode,
  fall back to **H.264 main profile** (set on the camera's Video
  Settings page) — universally supported.

### Timestamp / PTS bug

Hikvision cameras frequently emit RTSP packets with broken or non-monotonic
PTS values. Symptom: FFmpeg warnings like `non monotonically increasing
dts`, or MediaMTX rejecting the stream. Fix: always include
`-fflags +genpts` in any FFmpeg pipeline consuming a Hikvision camera
(see the recommended FFmpeg command above — it's already there).

### ONVIF vs RTSP

Hikvision supports both ONVIF (for PTZ and discovery) and RTSP (for media).
For MediaMTX integration:

- Use **RTSP** for the media stream
- Use **ONVIF** (via a separate client) for PTZ control if needed
- Don't try to control media via ONVIF — RTSP is the right tool

### Hikvision + MediaMTX + FFmpeg pipeline

```yaml
# mediamtx.yml — Hikvision-specific example
paths:
  cam1_raw:                  # Hikvision H.265 main stream (default)
    runOnAvailable: >
      ffmpeg -hwaccel cuda -hwaccel_output_format cuda -extra_hw_frames 8
        -fflags +genpts -rtsp_transport tcp
        -i rtsp://admin:pass123@192.168.1.64:554/Streaming/Channels/101
        -c:v h264_nvenc -profile:v high -level:v 4.2
        -pix_fmt yuv420p -preset p4 -tune ll
        -rc cbr -b:v 4M -maxrate 4M -bufsize 2M
        -g 50 -keyint_min 50 -sc_threshold 0 -bf 0
        -r 25 -vf "scale_npp=1920:1080:interp_algo=super"
        -c:a aac -b:a 128k -ar 48000 -ac 2
        -f rtsp -muxdelay 0 -muxpreload 0
        rtsp://localhost:$RTSP_PORT/cam1_normalized
    runOnAvailableRestart: true
    runOnUnavailable: pkill -f "ffmpeg.*cam1_raw" || true

  cam1_sub:                  # Hikvision H.264 sub stream — direct passthrough
    source: rtsp://admin:pass123@192.168.1.64:554/Streaming/Channels/102
    sourceOnDemand: false

  cam1_normalized:
    record: true
    recordPath: /recordings/cam1/%Y-%m-%d_%H-%M-%S-%f
    recordFormat: fmp4
    recordSegmentDuration: 1h
    recordDeleteAfter: 168h
```

The `cam1_sub` path uses the camera's own sub stream — no FFmpeg
transcoding needed. Use it for AI consumption if its resolution (typically
640x360 or 704x576) is sufficient.

---

# Part 4 — Summary

## Strengths and Limitations

| ✅ Strengths | ⚠️ Limitations |
|---|---|
| Single binary, zero deps — trivial to deploy | Single process = single point of failure |
| ~30 internal packages — small enough to understand | No transcoding — codec compatibility is the caller's problem |
| Zero pixel processing — no GPU, low CPU per reader | File-based recording only (no S3/MinIO native) |
| Path abstraction is clean and minimal | Hooks are fire-and-forget, no ack |
| 10+ protocols supported in/out | WebRTC ICE complexity (but encapsulated) |
| Hot reload without dropping clients | HLS scalability requires CDN (HLS-only) |
| Three auth modes (internal/HTTP/JWT) | No native MJPEG read endpoint |
| Hooks make integration ergonomic (incl. transcode spawning) | No B-frames allowed in transcoded streams (live streaming limit) |
| Control API + Prometheus metrics built in | Dual-layer architecture adds 200–500 ms transcode latency |
| MIT license — commercial-friendly | |
| NVIDIA-documented FFmpeg transcoding pipeline integrates cleanly | |
| Frigate-style dual-stream architecture maps directly to MediaMTX paths | |
| Hikvision H.265 → H.264 unified-format pattern via FFmpeg hook | |

## References

- **MediaMTX repo**: https://github.com/bluenviron/mediamtx
- **MediaMTX docs**: https://mediamtx.org/docs/features/architecture
- **Configuration reference**: https://mediamtx.org/docs/references/configuration-file
- **Control API reference**: https://mediamtx.org/docs/references/control-api
- **Auth tutorial**: https://mediamtx.org/docs/features/authentication
- **Scalability patterns**: https://mediamtx.org/docs/features/scalability
- **Re-encoding feature page**: https://mediamtx.org/docs/features/remuxing-reencoding-compression
- **NVIDIA FFmpeg transcoding guide**: https://developer.nvidia.com/blog/nvidia-ffmpeg-transcoding-guide/
- **Frigate camera configuration**: https://docs.frigate.video/configuration/cameras
- **Twitch broadcasting guidelines**: https://help.twitch.tv/s/article/broadcasting-guidelines
- **Virex `ai-backend` plan** (this doc's architectural context):
  `/home/loadingcloud001/.local/share/kilo/plans/1784975160518-ai-live-stream-inference-plan.md`