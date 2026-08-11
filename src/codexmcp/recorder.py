"""Persist codex run event streams to disk so the monitor GUI can follow them live.

Every ``codex`` tool invocation becomes a *run* backed by two files:

``<run_id>.meta.json``
    Small JSON document with status and parameters. Rewritten as the run
    progresses (atomically, so readers never observe a half-written file).

``<run_id>.jsonl``
    Append-only event log. One JSON object per line, ``{"t": <iso>, "e": <codex event>}``
    for parsed events and ``{"t": <iso>, "raw": <text>}`` for lines codex emitted
    that were not valid JSON.

Recording is strictly best-effort: any failure disables the recorder for the rest
of the run instead of propagating into the MCP tool.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Older runs are pruned once per process so the directory cannot grow forever.
MAX_RETAINED_RUNS = 500

# The meta file is rewritten at most this often while events stream in.
_META_FLUSH_INTERVAL = 1.0

_pruned = False


def runs_dir() -> Path:
    """Directory holding run files. Override the root with ``CODEXMCP_HOME``."""
    root = os.environ.get("CODEXMCP_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".codexmcp"
    return base / "runs"


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string with millisecond precision."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _prune_once(directory: Path) -> None:
    """Drop the oldest runs beyond ``MAX_RETAINED_RUNS``."""
    global _pruned
    if _pruned:
        return
    _pruned = True
    try:
        metas = sorted(
            directory.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for stale in metas[MAX_RETAINED_RUNS:]:
            run_id = stale.name[: -len(".meta.json")]
            for suffix in (".meta.json", ".jsonl"):
                try:
                    (directory / f"{run_id}{suffix}").unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        pass


class RunRecorder:
    """Records one codex invocation. Never raises."""

    def __init__(self, run_id: str, directory: Path, meta: Dict[str, Any]) -> None:
        self.run_id = run_id
        self._dir = directory
        self._meta = meta
        self._meta_path = directory / f"{run_id}.meta.json"
        self._events_path = directory / f"{run_id}.jsonl"
        self._events: Optional[Any] = None
        self._disabled = False
        self._last_flush = 0.0

    @classmethod
    def start(cls, params: Dict[str, Any]) -> "RunRecorder":
        """Create a run and write its initial meta file."""
        started = utcnow()
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        directory = runs_dir()
        meta: Dict[str, Any] = {
            "run_id": run_id,
            "status": "running",
            "started_at": started,
            "updated_at": started,
            "finished_at": None,
            "pid": os.getpid(),
            "event_count": 0,
            "session_id": None,
            "error": None,
            **params,
        }
        recorder = cls(run_id, directory, meta)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _prune_once(directory)
            recorder._events = open(
                recorder._events_path, "a", encoding="utf-8", buffering=1
            )
            recorder._write_meta()
        except Exception:
            recorder._disable()
        return recorder

    def _disable(self) -> None:
        self._disabled = True
        events, self._events = self._events, None
        if events is not None:
            try:
                events.close()
            except Exception:
                pass

    def _write_meta(self) -> None:
        """Write the meta file atomically so readers see either old or new content."""
        payload = json.dumps(self._meta, ensure_ascii=False, indent=2)
        tmp = self._dir / f"{self.run_id}.meta.tmp"
        tmp.write_text(payload, encoding="utf-8")
        # os.replace can transiently fail on Windows while a reader holds the
        # destination open; a couple of retries makes that effectively invisible.
        for attempt in range(3):
            try:
                os.replace(tmp, self._meta_path)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
        self._last_flush = time.monotonic()

    def _flush_meta(self, force: bool = False) -> None:
        if self._disabled:
            return
        if not force and time.monotonic() - self._last_flush < _META_FLUSH_INTERVAL:
            return
        try:
            self._write_meta()
        except Exception:
            self._disable()

    def record(self, raw_line: str, parsed: Optional[Dict[str, Any]] = None) -> None:
        """Append one line of codex output to the event log."""
        if self._disabled or self._events is None:
            return
        entry: Dict[str, Any] = {"t": utcnow()}
        if parsed is None:
            entry["raw"] = raw_line
        else:
            entry["e"] = parsed
        try:
            self._events.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            self._disable()
            return
        self._meta["event_count"] += 1
        self._meta["updated_at"] = entry["t"]
        self._flush_meta()

    def set_session_id(self, session_id: str) -> None:
        """Record the codex thread id as soon as it is known."""
        if self._disabled or self._meta.get("session_id") == session_id:
            return
        self._meta["session_id"] = session_id
        self._flush_meta(force=True)

    def finish(
        self, success: bool, error: str = "", agent_messages: str = ""
    ) -> None:
        """Finalize the run. Safe to call from an exception handler."""
        if self._disabled:
            return
        now = utcnow()
        self._meta["status"] = "completed" if success else "failed"
        self._meta["updated_at"] = now
        self._meta["finished_at"] = now
        # Only surface an error for a run that actually failed. codex emits a
        # non-JSON banner line on every run, which the caller collects as a
        # warning; showing that on a successful run would be misleading.
        self._meta["error"] = None if success else ((error or "").strip() or None)
        self._meta["reply_chars"] = len(agent_messages)
        try:
            self._write_meta()
        except Exception:
            pass
        self._disable()


def _read_meta(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_runs() -> List[Dict[str, Any]]:
    """All recorded runs, newest first."""
    directory = runs_dir()
    if not directory.is_dir():
        return []
    runs = [
        meta
        for path in directory.glob("*.meta.json")
        if (meta := _read_meta(path)) is not None
    ]
    runs.sort(key=lambda meta: meta.get("started_at") or "", reverse=True)
    return runs


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Meta for a single run, or ``None`` when the id is unknown."""
    if not _is_safe_run_id(run_id):
        return None
    return _read_meta(runs_dir() / f"{run_id}.meta.json")


def read_events(run_id: str, offset: int = 0) -> Dict[str, Any]:
    """Events for a run starting at line ``offset``.

    Returns the events plus the line cursor to pass as the next ``offset``. The
    cursor counts *lines* rather than returned events, so a line that fails to
    parse cannot drift a polling reader out of sync. An incomplete trailing line
    is left for the next poll so readers never see a half-flushed event.
    """
    result: Dict[str, Any] = {"events": [], "next_offset": offset}
    if not _is_safe_run_id(run_id):
        return result
    path = runs_dir() / f"{run_id}.jsonl"
    cursor = offset
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index < offset:
                    continue
                if not line.endswith("\n"):
                    break
                cursor = index + 1
                try:
                    result["events"].append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return result
    result["next_offset"] = cursor
    return result


def _is_safe_run_id(run_id: str) -> bool:
    """Guard the HTTP surface against path traversal via the run id."""
    return bool(run_id) and all(char.isalnum() or char == "-" for char in run_id)
