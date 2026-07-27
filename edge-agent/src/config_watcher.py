# SPDX-License-Identifier: Apache-2.0
"""`workers.yaml` file watcher for the edge-agent.

When `workers.yaml` is rewritten (by the portal-writing path or by a
direct edit), the watcher re-parses it, calls `tier_classify.classify`
against the last-applied bundle, and dispatches the resulting
`TierReport` to a caller-supplied handler.

Two implementations live here:
  * `InotifyConfigWatcher` — uses `watchdog.observers.Observer` for
    real inotify events on Linux. Default.
  * `PollingConfigWatcher` — fallback that stats the file every
    `poll_interval_sec`. Used when inotify is unreliable (some
    Docker bind mounts, macOS).

Both yield events with shape `(path, mtime_ns)`; the handler does the
parsing + classification. Putting parsing outside the handler means the
classifier is testable without filesystem I/O.

On the edge-agent side, `config_pull_loop` is the orchestrator: it
periodically fetches `/api/edge/config` AND wires a watcher on
`workers.yaml` (the local fallback). Both code paths funnel through
the same `reconcile_fn` callback so the tier-aware apply step runs
whether the change came from the portal or a file edit.

The Frigate analogy: `config_watcher` is the inotify frontend,
`tier_classifier` is the equivalence-class mapper, and
`reconcile.apply_tier_report` is the per-camera action dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from src.config_pull import EdgeConfigBundle
from src.tier_classifier import (
    TierReport,
    classify,
    parse_workers_yaml,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WatcherEvent:
    """Single file-mtime observation; the orchestrator parses + diffs."""

    path: str
    mtime_ns: int


# An async handler that receives a parsed bundle + tier report.
TierHandler = Callable[[EdgeConfigBundle, TierReport], Awaitable[None]]


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------
class ConfigWatcher(ABC):
    """Base interface so the orchestrator can swap backends."""

    @abstractmethod
    async def run(self, handler: TierHandler) -> None:  # pragma: no cover
        ...

    @abstractmethod
    def stop(self) -> None:
        ...


# ---------------------------------------------------------------------------
# watchdog (inotify) backend — Linux only
# ---------------------------------------------------------------------------
class InotifyConfigWatcher(ConfigWatcher):
    """Uses `watchdog.observers.Observer` to watch workers.yaml.

    Receives MODIFIED events from watchdog's thread and dispatches them
    to the asyncio loop via `loop.call_soon_threadsafe`. The handler is
    awaited in the loop so we don't serialize Pydantic work in the
    inotify thread.

    Failures inside `handler` are logged and swallowed so a transient
    parse error doesn't kill the loop.
    """

    def __init__(
        self,
        yaml_path: str,
        *,
        debounce_ms: int = 200,
    ) -> None:
        self._path = yaml_path
        self._debounce_ms = debounce_ms
        self._stopped = False

    async def run(self, handler: TierHandler) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        loop = asyncio.get_running_loop()
        path_obj = Path(self._path)
        if not path_obj.exists():
            logger.warning("config_watcher_file_missing", path=self._path)

        last_dispatch_ms: float = 0.0
        pending: asyncio.Event = asyncio.Event()

        class _Handler(FileSystemEventHandler):  # type: ignore[misc]
            def on_modified(self, event) -> None:  # noqa: ANN001
                if event.is_directory:
                    return
                # Compare paths to handle watchdir-level events.
                if Path(str(event.src_path)).resolve() != path_obj.resolve():
                    return
                loop.call_soon_threadsafe(_schedule)

        def _schedule() -> None:
            nonlocal last_dispatch_ms
            now = time.monotonic() * 1000
            if now - last_dispatch_ms < self._debounce_ms:
                return
            last_dispatch_ms = now
            pending.set()

        observer = Observer()
        parent = path_obj.parent
        observer.schedule(_Handler(), str(parent), recursive=False)
        observer.start()

        try:
            while not self._stopped:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(pending.wait(), timeout=1.0)
                pending.clear()

                try:
                    bundle = parse_workers_yaml(self._path)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "config_watcher_yaml_invalid",
                        path=self._path,
                        error=str(e)[:300],
                    )
                    continue

                previous = _last_applied.get(self._path)
                tier = None if previous is None else classify(previous, bundle)
                _last_applied[self._path] = bundle

                # First-ever event for this file: baseline, no handler call.
                if tier is None or not tier.has_changes:
                    continue
                try:
                    await handler(bundle, tier)
                except Exception:  # noqa: BLE001
                    logger.exception("config_watcher_handler_failed")
        finally:
            observer.stop()
            observer.join(timeout=2.0)

    def stop(self) -> None:
        self._stopped = True


# ---------------------------------------------------------------------------
# Polling fallback
# ---------------------------------------------------------------------------
class PollingConfigWatcher(ConfigWatcher):
    """Stats the file every `poll_interval_sec`; emits on mtime change.

    Used when inotify isn't available (Docker bind mount, macOS, or
    when watchdog fails to import). Slightly higher lag (≤ poll
    interval) but functionally equivalent.
    """

    def __init__(
        self,
        yaml_path: str,
        *,
        poll_interval_sec: float = 1.0,
    ) -> None:
        self._path = yaml_path
        self._interval = poll_interval_sec
        self._stopped = False
        self._last_mtime_ns: int | None = None
        self._last_content_hash: str | None = None

    async def run(self, handler: TierHandler) -> None:
        path_obj = Path(self._path)
        # Establish baseline on the FIRST observation so subsequent
        # changes have something to diff against. Detection uses BOTH
        # mtime and content-hash because Docker bind-mounts can have
        # stale stat() data while open() reads fresh content.
        first_iter = True
        while not self._stopped:
            try:
                mtime_ns = path_obj.stat().st_mtime_ns
                raw_bytes = path_obj.read_bytes()
                content_hash = hashlib.sha256(raw_bytes).hexdigest()
            except FileNotFoundError:
                logger.warning("config_watcher_file_missing", path=self._path)
                await asyncio.sleep(self._interval)
                continue

            mtime_changed = mtime_ns != self._last_mtime_ns
            hash_changed = content_hash != self._last_content_hash

            if (
                not first_iter
                and not mtime_changed
                and not hash_changed
                and self._last_mtime_ns is not None
            ):
                await asyncio.sleep(self._interval)
                continue

            try:
                bundle = parse_workers_yaml(self._path)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "config_watcher_yaml_invalid",
                    path=self._path,
                    error=str(e)[:300],
                )
                await asyncio.sleep(self._interval)
                continue

            previous = _last_applied.get(self._path)
            tier = None if previous is None else classify(previous, bundle)
            _last_applied[self._path] = bundle
            self._last_mtime_ns = mtime_ns
            self._last_content_hash = content_hash

            # First iteration: just establish baseline, no diff.
            # Subsequent iterations: diff against baseline.
            if not first_iter and previous is not None and tier.has_changes:
                try:
                    await handler(bundle, tier)
                except Exception:  # noqa: BLE001
                    logger.exception("config_watcher_handler_failed")

            first_iter = False
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._stopped = True


# ---------------------------------------------------------------------------
# Module-level cache of last-applied bundles, keyed by yaml_path.
# Each watcher instance shares this so re-runs don't lose state.
# ---------------------------------------------------------------------------
_last_applied: dict[str, EdgeConfigBundle] = {}


def reset_last_applied_cache() -> None:
    """For tests: clear the in-memory `last_applied` map."""
    _last_applied.clear()


def get_last_applied(yaml_path: str) -> EdgeConfigBundle | None:
    """Return the cached EdgeConfigBundle for `yaml_path`, or None."""
    return _last_applied.get(yaml_path)


def set_last_applied(yaml_path: str, bundle: EdgeConfigBundle) -> None:
    _last_applied[yaml_path] = bundle


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def make_config_watcher(yaml_path: str, *, force_polling: bool = False) -> ConfigWatcher:
    """Return InotifyConfigWatcher unless `force_polling=True` or
    watchdog isn't importable.
    """
    if force_polling:
        return PollingConfigWatcher(yaml_path)
    try:
        import watchdog  # noqa: F401
    except ImportError:
        logger.info("config_watcher_watchdog_unavailable_using_polling")
        return PollingConfigWatcher(yaml_path)
    return InotifyConfigWatcher(yaml_path)


__all__: tuple[str, ...] = (
    "ConfigWatcher",
    "InotifyConfigWatcher",
    "PollingConfigWatcher",
    "WatcherEvent",
    "TierHandler",
    "make_config_watcher",
    "reset_last_applied_cache",
    "get_last_applied",
    "set_last_applied",
)
