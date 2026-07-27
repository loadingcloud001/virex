# SPDX-License-Identifier: Apache-2.0
"""Reconcile portal's view of this edge node with local reality.

Three artifacts are kept in sync on full reconcile (Tier D):
  1. `/etc/virex/mediamtx.yml` — full MediaMTX config rendered from
     `templates/mediamtx.yml.j2` (auth, ports, recording defaults +
     the per-camera `paths:` block).
  2. `/etc/virex/docker-compose.transcoder.yml` — one FFmpeg sidecar
     per camera, rendered from `templates/docker-compose.transcoder.yml.j2`.
  3. `/etc/virex/docker-compose.worker.yml` — one service per camera,
     rendered from `templates/docker-compose.worker.yml.j2`.

After writing each file, the corresponding action is taken:
  * `docker compose -f <worker-compose> up -d --remove-orphans`
  * `docker compose -f <transcoder-compose> up -d --remove-orphans`
  * `docker restart mediamtx` — only when its file content changed.

Hot-reload (Tier A/B)
---------------------
For Tier-A/B changes (no container / MediaMTX restart), `apply_tier_report`
delegates to the affected worker's admin endpoint:

    POST /admin/reload  →  worker atomically swaps HotConfig + reconnects
                            RTSP if source_rtsp changed

Tier-C changes are routed to:
  * `record` toggle → rewrite mediamtx.yml (the path block) and
    `docker restart mediamtx` (single ~5 s blip on all paths).
  * transcoder params → `docker compose -f transcoder up -d --force-recreate
    transcoder-<path>` per sidecar.

Tier-D (add/remove/rename) falls through to the full reconcile path.

MediaMTX does NOT support YAML includes; the only sane sustainable
approach is to render the entire config file and restart the container
when paths change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Iterable
from pathlib import Path

import structlog
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from src.config import Settings
from src.config_pull import EdgeConfigBundle
from src.tier_classifier import (
    TierReport,
    cameras_to_reload,
    tier_requires_worker_reload,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WORKER_RESTART_POLICY: str = "unless-stopped"
WORKER_ADMIN_PORT_DEFAULT: int = 32000


def _env_template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_env_template_path())),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


# ---------------------------------------------------------------------------
# Renderers (unchanged from before; called by full reconcile path)
# ---------------------------------------------------------------------------
def render_worker_compose(bundle: EdgeConfigBundle, settings: Settings) -> str:
    """Render `docker-compose.worker.yml` with one service per camera."""
    env = _make_env()
    tpl = env.get_template("docker-compose.worker.yml.j2")
    base_port = WORKER_ADMIN_PORT_DEFAULT
    cameras = []
    for idx, c in enumerate(bundle.cameras, start=1):
        # Each worker gets a unique admin port — 32000, 32001, 32002, …
        # so multiple workers on the same host (network_mode=host) don't
        # collide. Edge-agent records this mapping in `_admin_port_by_path`
        # at render time so `worker_reload()` can find the right URL.
        cameras.append({
            "service_name": f"worker-{c.mtx_path}",
            "mtx_path": c.mtx_path,
            "camera_json": json.dumps(c.model_dump()),
            "label": f"virex-camera={c.mtx_path}",
            "admin_port": base_port + idx - 1,
        })
    return tpl.render(
        cameras=cameras,
        worker_image=settings.worker_image,
        restart_policy=WORKER_RESTART_POLICY,
        worker_admin_port=base_port,
        state_dir_host=settings.state_dir,
    )


def worker_admin_port_for_path(mtx_path: str) -> int:
    """Return the admin port of the worker for `mtx_path`.

    Edge-agent calls this after `render_worker_compose` so it knows
    where to send `POST /admin/reload`. The mapping is `worker-X` →
    `32000 + camera_index`.
    """
    # Simple deterministic mapping: indexed by camera enumeration
    # order. We rebuild the same list the renderer used, ensuring
    # worker-X admin port = 32000 + X for X = 0, 1, 2, ...
    settings = Settings()
    # Read the rendered compose file path on disk to find the index.
    # This is robust against ordering changes in `bundle.cameras`.
    compose_path = Path(settings.edge_compose_path)
    if not compose_path.exists():
        return WORKER_ADMIN_PORT_DEFAULT
    import yaml as _yaml  # noqa: PLC0415
    data = _yaml.safe_load(compose_path.read_text())
    services = (data or {}).get("services", {})
    order = sorted(services.keys())
    try:
        idx = order.index(f"worker-{mtx_path}")
    except ValueError:
        return WORKER_ADMIN_PORT_DEFAULT
    return WORKER_ADMIN_PORT_DEFAULT + idx


def render_mediamtx_yml(bundle: EdgeConfigBundle, settings: Settings) -> str:
    """Render the full MediaMTX YAML config from the v1 template.

    Each camera contributes BOTH a `_raw` entry (passthrough from the
    camera, source URL injected) and a `_h264` entry (normalized output
    published by the FFmpeg transcoder sidecar). Recording is enabled
    on the `_h264` path only — recording the raw stream would double
    disk usage with no quality benefit since the transcoder is lossless
    in our pipeline (libx264 -preset ultrafast @ 4 Mbps ≈ source).
    """
    env = _make_env()
    tpl = env.get_template("mediamtx.yml.j2")
    cameras = [
        {
            "mtx_path": c.mtx_path,
            "source_rtsp": c.source_rtsp,
            "record": c.record,
        }
        for c in bundle.cameras
    ]
    return tpl.render(
        cameras=cameras,
        h264_suffix=settings.mediamtx_h264_suffix,
    )


def render_transcoder_compose(bundle: EdgeConfigBundle, settings: Settings) -> str:
    """Render `docker-compose.transcoder.yml` with one service per camera."""
    env = _make_env()
    tpl = env.get_template("docker-compose.transcoder.yml.j2")
    cameras = [
        {
            "mtx_path": c.mtx_path,
        }
        for c in bundle.cameras
    ]
    return tpl.render(
        cameras=cameras,
        mediamtx_rtsp_port=settings.mediamtx_rtsp_port,
        h264_suffix=settings.mediamtx_h264_suffix,
        restart_policy=WORKER_RESTART_POLICY,
    )


def safe_write(path: Path, content: str) -> bool:
    """Write file atomically; True if content changed (caller decides to restart)."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return True


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------
# Cap subprocess runtime so a hung `docker compose up -d` cannot
# wedge the entire reconcile loop indefinitely. 120s is generous for
# image pulls; cold-start pulls of large images may need longer via
# DOCKER_COMPOSE_TIMEOUT_SEC env override.
DOCKER_SUBPROCESS_TIMEOUT_SEC: float = float(os.environ.get("DOCKER_SUBPROCESS_TIMEOUT_SEC", "120"))


async def run_subprocess(cmd: list[str]) -> tuple[int, bytes, bytes]:
    """Run `cmd` with a hard timeout; kill the child on expiry.

    Without a timeout, a wedged dockerd or stuck image pull blocks the
    entire edge-agent (the watcher loop awaits this inline). We cap
    output at 4 MiB per stream to bound memory during cold pulls.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=DOCKER_SUBPROCESS_TIMEOUT_SEC
        )
    except TimeoutError:
        # Kill the child process tree so it doesn't linger.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        logger.error(
            "subprocess_timeout",
            cmd=cmd[0],
            timeout_sec=DOCKER_SUBPROCESS_TIMEOUT_SEC,
        )
        return 124, b"", b"subprocess timed out"
    # Bound memory: keep only the tail so we don't OOM on cold pulls.
    MAX_OUT = 4 * 1024 * 1024
    if len(out) > MAX_OUT:
        out = out[-MAX_OUT:]
    if len(err) > MAX_OUT:
        err = err[-MAX_OUT:]
    return (proc.returncode if proc.returncode is not None else 0), out, err


async def run_docker_compose_up(compose_path: Path) -> int:
    """Idempotent `docker compose up -d --remove-orphans`.

    `project_directory` is the directory containing `workers.yaml` so any
    relative paths inside the compose file resolve to a real host path,
    not the docker daemon's cwd. Both the compose file and the project
    directory live under the bind-mounted /etc/virex (== state/ on host),
    so we reuse its parent as the project directory.

    Also pass `--env-file ../.env` so docker compose expands ${VAR} in
    any environment references inside the rendered compose file. The
    env file lives at `deploy/edge/.env` (one level above state/); we
    resolve the canonical path via the state_dir setting to keep this
    location-independent.
    """
    project_dir = compose_path.parent.resolve()
    state_dir = Path(settings.state_dir).resolve()
    env_file = (state_dir.parent / ".env").resolve()
    rc, out, err = await run_subprocess(
        [
            "docker",
            "compose",
            "--project-directory",
            str(project_dir),
            "--env-file",
            str(env_file),
            "-f",
            str(compose_path),
            "up",
            "-d",
            "--remove-orphans",
        ]
    )
    if rc != 0:
        logger.error(
            "docker_compose_up_failed",
            rc=rc,
            stderr=err.decode("utf-8", "replace")[:500],
        )
    else:
        logger.info(
            "docker_compose_up_done",
            stdout=out.decode("utf-8", "replace")[:200],
        )
    return rc


async def restart_mediamtx() -> int:
    rc, _out, err = await run_subprocess(["docker", "restart", "mediamtx"])
    if rc != 0:
        logger.error(
            "mediamtx_restart_failed",
            stderr=err.decode("utf-8", "replace")[:500],
        )
    else:
        logger.info("mediamtx_restarted")
    return rc


async def recreate_transcoder(mtx_path: str) -> int:
    """Force-recreate a single FFmpeg transcoder sidecar (~5 s blip)."""
    rc, _out, err = await run_subprocess(
        [
            "docker",
            "compose",
            "-f",
            str(Path("/etc/virex/docker-compose.transcoder.yml").resolve()),
            "up",
            "-d",
            "--force-recreate",
            f"transcoder-{mtx_path}",
        ]
    )
    if rc != 0:
        logger.error(
            "transcoder_restart_failed",
            path=mtx_path,
            stderr=err.decode("utf-8", "replace")[:500],
        )
    return rc


async def worker_reload(worker_url: str, *, mtx_path: str = "") -> int:
    """POST `/admin/reload` to a single worker.

    Worker admin port (default 32000 + camera_index) is on the host
    network — host loopback on edge-agent's container resolves to the
    same loopback the worker is bound to. Path is `workers/<mtx_path>`
    mapped to IP via Docker DNS in multi-host setups, but for v1 pilot
    all workers run on the same host.
    """
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{worker_url}/admin/reload")
        except httpx.HTTPError as e:
            logger.warning(
                "worker_reload_failed",
                url=worker_url,
                mtx_path=mtx_path,
                error=str(e),
            )
            return 1
        if resp.status_code >= 400:
            logger.warning(
                "worker_reload_error",
                url=worker_url,
                mtx_path=mtx_path,
                status=resp.status_code,
                body=resp.text[:200],
            )
            return 1
        logger.info("worker_reload_ok", url=worker_url, mtx_path=mtx_path)
    return 0


# ---------------------------------------------------------------------------
# Full reconcile (Tier D) — keeps the old behaviour for add/remove.
# ---------------------------------------------------------------------------
async def ensure_state_symlinks(state_dir: Path) -> None:
    """Ensure `state/.env` points to `../.env` so docker compose auto-loads it.

    The single source of truth for secrets is `deploy/edge/.env`. We symlink
    it into the compose project directory (`state/`) so both the rendered
    worker compose (whose `env_file: .env` resolves relative to the project
    directory) and the direct `--env-file ../.env` invocation in
    `run_docker_compose_up` reach the same file. Safe to call repeatedly.
    """
    target = state_dir.parent / ".env"
    link = state_dir / ".env"
    if target.is_file() and (not link.exists() or link.is_symlink()):
        if link.is_symlink() or link.exists():
            link.unlink()
        try:
            link.symlink_to(target)
            logger.info("state_env_symlink_created", link=str(link), target=str(target))
        except FileExistsError:
            pass


async def run_reconcile(bundle: EdgeConfigBundle) -> None:
    """Apply the bundle to this edge node — full render + container restart."""
    settings = Settings()
    compose_path = Path(settings.edge_compose_path)
    mediamtx_path = Path(settings.mediamtx_main_path)
    transcoder_compose_path = compose_path.parent / "docker-compose.transcoder.yml"
    state_dir = compose_path.parent

    compose_text = render_worker_compose(bundle, settings)
    mediamtx_text = render_mediamtx_yml(bundle, settings)
    transcoder_text = render_transcoder_compose(bundle, settings)

    compose_changed = safe_write(compose_path, compose_text)
    mediamtx_changed = safe_write(mediamtx_path, mediamtx_text)
    transcoder_changed = safe_write(transcoder_compose_path, transcoder_text)

    logger.info(
        "reconcile_rendered",
        cameras=len(bundle.cameras),
        compose_changed=compose_changed,
        mediamtx_changed=mediamtx_changed,
        transcoder_changed=transcoder_changed,
    )

    # Ensure state/.env -> ../.env symlink so the worker containers see
    # MINIO_*/MQTT_* secrets at startup. The actual rotate lives in
    # deploy/edge/.env; this symlink keeps the rendered worker compose
    # resolving correctly without duplicating secrets.
    await ensure_state_symlinks(state_dir)

    # Order matters: bring up transcoders before workers so `_h264` paths
    # are already accepting data when workers start pulling from them.
    await run_docker_compose_up(transcoder_compose_path)
    await run_docker_compose_up(compose_path)
    if mediamtx_changed:
        await restart_mediamtx()


# ---------------------------------------------------------------------------
# Tier-aware apply — for hot-reload changes that don't need full reconcile.
# ---------------------------------------------------------------------------
async def apply_tier_report(
    bundle: EdgeConfigBundle,
    report: TierReport,
    *,
    worker_admin_port: int = WORKER_ADMIN_PORT_DEFAULT,
) -> None:
    """Apply a TierReport from the watcher / pull loop.

    Order:
      1. Tier A/B — trigger per-worker admin reload (atomic swap inside
         each worker; per-camera RTSP reconnect for source_rtsp).
      2. Tier C — restart MediaMTX (if `record` flags changed) and any
         affected transcoder sidecars.
      3. Tier D — full reconcile, which covers the transcoder / worker
         add/remove automatically.

    Note: this function is a no-op when `report.has_changes` is False.
    """
    if not report.has_changes:
        return

    logger.info(
        "apply_tier_report",
        added=list(report.added),
        removed=list(report.removed),
        renamed=list(report.renamed),
        tier_a=list(report.tier_a_per_camera),
        tier_b=list(report.tier_b_per_camera),
        tier_c_paths=list(report.tier_c_paths),
        tier_c_transcoders=list(report.tier_c_transcoders),
    )

    # ---- Tier A/B: per-worker admin reload ----
    # IMPORTANT: this only works if the worker compose file exists on
    # disk (rendered by run_reconcile). On the first ever Tier-A apply
    # before any reconcile has rendered the compose file, we must
    # escalate to a full reconcile so the workers actually come up.
    settings = Settings()
    compose_path = Path(settings.edge_compose_path)
    if tier_requires_worker_reload(report) and not compose_path.exists():
        logger.warning(
            "tier_ab_escalate_to_reconcile",
            reason="worker compose not yet rendered; full reconcile required",
        )
        await run_reconcile(bundle)
        return
    if tier_requires_worker_reload(report):
        # Each worker has its own admin port (32000 + camera_index).
        # Send reload to each affected worker.
        for path in cameras_to_reload(report):
            port = worker_admin_port_for_path(path)
            worker_url = f"http://127.0.0.1:{port}"
            await worker_reload(worker_url, mtx_path=path)

    # ---- Tier C: MediaMTX `record` change ----
    if report.tier_c_paths:
        # Render full mediamtx + restart MediaMTX (single ~5 s blip).
        settings = Settings()
        mediamtx_path = Path(settings.mediamtx_main_path)
        mediamtx_text = render_mediamtx_yml(bundle, settings)
        if safe_write(mediamtx_path, mediamtx_text):
            await restart_mediamtx()

    # ---- Tier C: per-sidecar transcoder restart ----
    for path in report.tier_c_transcoders:
        await recreate_transcoder(path)

    # ---- Tier D: full reconcile (add/remove) ----
    if report.added or report.removed or report.renamed:
        logger.info(
            "tier_d_full_reconcile",
            added=len(report.added),
            removed=len(report.removed),
        )
        await run_reconcile(bundle)


__all__: Iterable[str] = (
    "render_worker_compose",
    "render_mediamtx_yml",
    "render_transcoder_compose",
    "safe_write",
    "run_docker_compose_up",
    "restart_mediamtx",
    "recreate_transcoder",
    "worker_reload",
    "run_reconcile",
    "apply_tier_report",
    "WORKER_RESTART_POLICY",
    "WORKER_ADMIN_PORT_DEFAULT",
)
