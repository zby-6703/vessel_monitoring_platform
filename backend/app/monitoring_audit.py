"""Bounded operational evidence without camera addresses or exception text.

Audit failures must never interrupt capture, lens control or clip completion.
Only explicitly enumerated fields are serialized; this is not a general logger.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading


AUDIT_PATH = Path(__file__).resolve().parents[2] / "data" / "logs" / "monitoring-events.jsonl"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
TAIL_BYTES = 128 * 1024
_lock = threading.RLock()

EVENTS = frozenset({
    "recording_settings_changed", "camera_start_requested", "camera_running",
    "camera_stopped", "camera_failed", "optical_phase", "recording_started",
    "recording_finished", "recording_failed", "recording_encoding", "zoom_observation",
})
REASONS = frozenset({
    "settings_saved", "source_stopped", "source_completed", "source_changed",
    "disabled", "settings_changed", "vessel_left", "max_duration",
    "max_clip_seconds", "resolution_changed", "source_discontinuity",
    "target_stationary", "target_ambiguous", "target_not_reacquired",
    "camera_unavailable", "camera_connection_failed", "camera_read_failed",
    "capture_failed", "capture_error", "model_failed", "inference_failed",
    "lens_unavailable", "zoom_unknown", "zoom_observed", "zoom_read_failed",
    "zoom_moving", "zoom_timeout", "stopped", "service_shutdown",
    "manual_stop", "operator_stop", "error", "failed", "encoding_failed",
    "storage_failed", "recording_error",
    "source_error", "writer_failed", "process_restarted", "storage_limit",
    "zoom_failed", "cycle_timeout",
    "zoom_retry",
})
_ENUMS = {
    "recording_mode": frozenset({"optical_cycle", "passage"}),
    "phase": frozenset({"overview", "initializing", "zooming_in", "zoomed",
                         "zooming_out", "rearming", "error"}),
    "source_type": frozenset({"camera", "local_video"}),
    "reason": REASONS,
}
_ID_KEYS = frozenset({"camera_id", "session_id", "recording_id"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_INTEGERS = frozenset({"frame_count", "generation"})
_RANGES = {"min_optical_zoom": (1., 40.), "target_height_ratio": (0., 1.),
           "zoom_factor": (1., 40.)}


def _safe_fields(fields: dict) -> dict:
    output = {}
    for key, value in fields.items():
        if key == "enabled" and type(value) is bool:
            output[key] = value
        elif key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
            output[key] = value
        elif key in _ID_KEYS and isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            output[key] = value
        elif key in _INTEGERS and type(value) is int and 0 <= value <= 2**53:
            output[key] = value
        elif key in _RANGES:
            if value is None and key == "zoom_factor":
                output[key] = None
            elif type(value) in {int, float} and math.isfinite(value):
                low, high = _RANGES[key]
                if low <= value <= high:
                    output[key] = value
    return output


def _rotate(path: Path) -> None:
    """Rotate only this logger's fixed sibling files, never a directory tree."""
    for index in range(BACKUP_COUNT, 0, -1):
        backup = path.with_name(f"{path.name}.{index}")
        previous = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        if previous.is_file():
            previous.replace(backup)


def record_event(event: str, **fields) -> None:
    """Append an allowlisted event. Unknown or unsafe fields are omitted."""
    if not isinstance(event, str) or event not in EVENTS:
        return
    try:
        item = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
                **_safe_fields(fields)}
        line = (json.dumps(item, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        with _lock:
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            if AUDIT_PATH.is_file() and AUDIT_PATH.stat().st_size + len(line) > MAX_BYTES:
                _rotate(AUDIT_PATH)
            with AUDIT_PATH.open("ab") as stream:
                stream.write(line)
    except (OSError, ValueError, TypeError, OverflowError):
        # Logging is observational. A read-only/full disk cannot stop a camera.
        return


def recent_events(limit: int = 50) -> list[dict]:
    """Return oldest-to-newest safe events from at most 128 KiB of the active file."""
    if type(limit) is not int:
        limit = 50
    limit = max(1, min(100, limit))
    try:
        with _lock, AUDIT_PATH.open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - TAIL_BYTES)
            stream.seek(start)
            content = stream.read(TAIL_BYTES)
        lines = content.splitlines()
        if start:
            lines = lines[1:]  # The first line may start in the middle of JSON.
        output = []
        for line in lines:
            try:
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("event") not in EVENTS:
                    continue
                observed = datetime.fromisoformat(value.get("timestamp", ""))
                if observed.tzinfo is None:
                    continue
                output.append({"timestamp": observed.astimezone(timezone.utc).isoformat(),
                               "event": value["event"], **_safe_fields(value)})
            except (ValueError, TypeError, AttributeError):
                continue
        return output[-limit:]
    except (OSError, ValueError):
        return []
