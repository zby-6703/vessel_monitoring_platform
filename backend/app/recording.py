"""Server-side, event-triggered full-frame and digital-focus passage recordings.

Source frames are supplied even when inference is skipped. Only new inference
observations count toward confirmation; media timestamps drive pre/post roll.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Literal
import uuid

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import get_settings
from .optical_recording import OpticalCycleMixin
from .monitoring_audit import record_event


class RecordingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = False
    recording_mode: Literal["optical_cycle", "passage"] = "optical_cycle"
    departure_confirm_seconds: float = Field(default=1, ge=.3, le=10)
    zoom_settle_timeout_seconds: float = Field(default=15, ge=3, le=60)
    overview_settle_seconds: float = Field(default=.5, ge=0, le=5)
    adaptive_zoom_enabled: bool = True
    target_height_ratio: float = Field(default=.70, ge=.15, le=.9)
    min_optical_zoom: float = Field(default=1.4, ge=1.1, le=40)
    max_optical_zoom: float = Field(default=8, ge=1.1, le=40)
    suppress_stationary: bool = True
    stationary_seconds: float = Field(default=15, ge=3, le=600)
    stationary_motion_threshold: float = Field(default=.015, ge=.002, le=.1)
    motion_confirm_seconds: float = Field(default=1, ge=.2, le=10)
    target_switch_margin: float = Field(default=.2, ge=0, le=1)
    zoom_adjust_cooldown_seconds: float = Field(default=3, ge=1, le=30)
    zoom_adjust_hysteresis: float = Field(default=.15, ge=.05, le=.5)
    target_acquire_timeout_seconds: float = Field(default=15, ge=3, le=120)
    pre_roll_seconds: float = Field(default=3, ge=0, le=20)
    post_roll_seconds: float = Field(default=5, ge=1, le=60)
    min_hits: int = Field(default=1, ge=1, le=20)
    confidence_threshold: float = Field(default=.45, ge=.1, le=1)
    max_clip_seconds: float = Field(default=300, ge=5, le=3600)
    max_storage_gb: float = Field(default=10, ge=.1, le=10000)
    retention_days: int = Field(default=30, ge=1, le=3650)
    focus_mode: Literal["auto", "manual"] = "auto"
    zoom_factor: float = Field(default=3, ge=1, le=40)
    center_x: float = Field(default=.5, ge=0, le=1)
    center_y: float = Field(default=.5, ge=0, le=1)
    padding: float = Field(default=.2, ge=0, le=1)
    # Realtime inference admission is independent of the recording switch.
    target_filter_enabled: bool = True
    min_target_height_ratio: float = Field(default=.06, ge=0, le=.8)
    min_target_width_ratio: float = Field(default=.12, ge=0, le=1)
    target_confirm_hits: int = Field(default=3, ge=1, le=30)
    target_confirm_seconds: float = Field(default=.4, ge=0, le=10)
    target_roi_left: float = Field(default=0, ge=0, le=1)
    target_roi_top: float = Field(default=0, ge=0, le=1)
    target_roi_right: float = Field(default=1, ge=0, le=1)
    target_roi_bottom: float = Field(default=1, ge=0, le=1)
    detail_interval_seconds: float = Field(default=.8, ge=0, le=30)
    max_detail_targets: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def validate_roll(self):
        if self.target_roi_left >= self.target_roi_right or self.target_roi_top >= self.target_roi_bottom:
            raise ValueError("监测区域左上坐标必须小于右下坐标")
        if self.min_optical_zoom > self.max_optical_zoom:
            raise ValueError("最小光学倍率不能大于最大倍率")
        if self.recording_mode == "optical_cycle" and self.zoom_factor <= 1:
            raise ValueError("光学循环的目标倍率必须大于 1×")
        if self.recording_mode == "passage" and self.zoom_factor > 8:
            raise ValueError("数字取景模式的放大倍率不能超过 8×")
        if self.pre_roll_seconds >= self.max_clip_seconds:
            raise ValueError("预录时长必须小于单段录像时长")
        return self


def _atomic_json(path: Path, document: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def focus_rectangle(width: int, height: int, vessels: list[dict], settings: RecordingSettings) -> dict:
    """Aspect-correct viewport enclosing every vessel, name and draught region."""
    fraction = 1 / settings.zoom_factor
    cx, cy = settings.center_x, settings.center_y
    if settings.focus_mode == "auto":
        bounds = []
        for vessel in vessels:
            for box in vessel.get("boxes", []):
                if box.get("type") not in {"ship", "ship_name_area", "waterline_area", "draft_mark"}:
                    continue
                xywh = box.get("xywh", [])
                if len(xywh) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in xywh):
                    x, y, w, h = xywh
                    if w > 0 and h > 0:
                        bounds.append((max(0, x / width), max(0, y / height), min(1, (x + w) / width), min(1, (y + h) / height)))
        if bounds:
            x1, y1 = min(b[0] for b in bounds), min(b[1] for b in bounds)
            x2, y2 = max(b[2] for b in bounds), max(b[3] for b in bounds)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            fraction = min(1., max(fraction, (x2 - x1) * (1 + settings.padding * 2), (y2 - y1) * (1 + settings.padding * 2)))
        else:
            fraction, cx, cy = 1., .5, .5
    return {"x": max(0., min(1 - fraction, cx - fraction / 2)), "y": max(0., min(1 - fraction, cy - fraction / 2)), "width": fraction, "height": fraction}


class ClipWriter:
    """Cheap capture-time encoding; H.264 conversion happens off capture threads."""
    def __init__(self, directory: Path, image: np.ndarray, fps: float, single: bool = False):
        self.directory = directory
        self.single = single
        height, width = image.shape[:2]
        # H.264 yuv420 needs even dimensions. Preserve the complete source view.
        self.size = (max(2, width // 2 * 2), max(2, height // 2 * 2))
        scale = min(1., 1280 / width, 720 / height)
        self.focus_size = (max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2))
        self.writers = []
        for kind, size in ([("cycle", self.size)] if single else [("original", self.size), ("focused", self.focus_size)]):
            writer = cv2.VideoWriter(str(directory / f"{kind}.capture.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
            self.writers.append(writer)
            if not writer.isOpened():
                self.close()
                raise RuntimeError("无法创建录像文件，请检查磁盘和视频编码器")

    def write(self, image: np.ndarray, rectangle: dict) -> None:
        if self.single:
            self.writers[0].write(cv2.resize(image, self.size))
            return
        h, w = image.shape[:2]
        x1, y1 = int(rectangle["x"] * w), int(rectangle["y"] * h)
        x2 = max(x1 + 1, min(w, math.ceil((rectangle["x"] + rectangle["width"]) * w)))
        y2 = max(y1 + 1, min(h, math.ceil((rectangle["y"] + rectangle["height"]) * h)))
        self.writers[0].write(cv2.resize(image, self.size))
        self.writers[1].write(cv2.resize(image[y1:y2, x1:x2], self.focus_size))

    def close(self) -> None:
        for writer in self.writers:
            writer.release()


class RecordingManager(OpticalCycleMixin):
    BUFFER_BYTES = 64 * 1024**2

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="passage-encoder")
        self.settings_path = self.root / "settings.json"
        self.settings = RecordingSettings()
        self.error: str | None = None
        if self.settings_path.is_file():
            try:
                self.settings = RecordingSettings.model_validate_json(self.settings_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.error = "录制配置无法读取，请重新保存设置"
        self.buffer: deque = deque()
        self.buffer_bytes = 0
        self.session_id: str | None = None
        self.source_name: str | None = None
        self.camera_id: str | None = None
        self.last_timestamp: float | None = None
        self.last_seen: float | None = None
        self.first_seen: float | None = None
        self.hits = 0
        self.vessels: list[dict] = []
        self.focus: dict | None = None
        self.active: dict | None = None
        self.catalog: dict[str, dict] = {}
        self.last_maintenance = 0.
        self._initialize_optical_cycle()
        recover = []
        for manifest in self.root.glob("*/manifest.json"):
            try:
                item = json.loads(manifest.read_text(encoding="utf-8"))
                if item["id"] != manifest.parent.name or not re.fullmatch(r"[a-f0-9]{32}", item["id"]):
                    continue
                started_at = datetime.fromisoformat(item["started_at"])
                if started_at.tzinfo is None or item["status"] not in {"recording", "processing", "completed", "interrupted", "failed"}:
                    continue
                if not isinstance(item["ship_names"], list) or not isinstance(item["track_ids"], list):
                    continue
                self.catalog[item["id"]] = item
                if item["status"] in {"recording", "processing"}:
                    item.update(status="processing", reason="process_restarted", ended_at=item.get("ended_at") or datetime.now(timezone.utc).isoformat())
                    _atomic_json(manifest, item)
                    recover.append(item["id"])
            except (ValueError, KeyError, OSError):
                continue
        # Workers may perform retention cleanup; finish populating the catalog
        # before any worker is allowed to iterate it.
        for recording_id in recover:
            self.executor.submit(self._encode, recording_id, "interrupted")

    def get_settings(self) -> dict:
        with self.lock:
            return self.settings.model_dump()

    def update_settings(self, value: dict | RecordingSettings) -> dict:
        with self.lock:
            settings = value if isinstance(value, RecordingSettings) else RecordingSettings.model_validate({**self.settings.model_dump(), **value})
            if settings.enabled and not shutil.which("ffmpeg"):
                raise ValueError("启用录制需要服务端安装 ffmpeg（含 libx264 编码器）")
            if settings.enabled and settings.recording_mode == "optical_cycle" and self.zoom_controller is not None:
                self._validate_zoom_policy(settings)
            _atomic_json(self.settings_path, settings.model_dump())
            if not settings.enabled:
                if self._optical_session:
                    self._halt_optical("disabled")
                else:
                    self._finish("disabled")
                self._clear_buffer()
                self.hits = 0
                self.last_seen = None
                self.first_seen = None
            if self._optical_session and settings.recording_mode != self.settings.recording_mode:
                self._halt_optical("settings_changed")
            self.settings = settings
            if self.phase == "error" and self._control_future is None and self.active is None:
                self._optical_session = False
                self._cycle_halt = False
                self._cycle_policy = None
                self._phase("overview")
            self.error = None
            self._maintenance(force=True)
            record_event("recording_settings_changed", enabled=settings.enabled,
                recording_mode=settings.recording_mode, min_optical_zoom=settings.min_optical_zoom,
                target_height_ratio=settings.target_height_ratio, reason="settings_saved")
            return self.settings.model_dump()

    def _clear_buffer(self):
        self.buffer.clear()
        self.buffer_bytes = 0

    def _buffer_frame(self, timestamp: float, image: np.ndarray):
        if self.settings.pre_roll_seconds <= 0:
            self._clear_buffer()
            return
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:
            self.buffer.append((timestamp, encoded))
            self.buffer_bytes += encoded.nbytes
        anchor = self.first_seen if self.hits and self.first_seen is not None else timestamp
        while self.buffer and (anchor - self.buffer[0][0] > self.settings.pre_roll_seconds or timestamp - self.buffer[0][0] >= self.settings.max_clip_seconds or self.buffer_bytes > self.BUFFER_BYTES):
            self.buffer_bytes -= self.buffer.popleft()[1].nbytes

    def process_frame(self, image: np.ndarray, *, camera_id: str, session_id: str, source_name: str, fps: float, timestamp: float, vessels: list[dict] | None = None, source_type: str = "local_video", observation_context: dict | None = None) -> None:
        with self.lock:
            try:
                self._process_frame(image, camera_id, session_id, source_name, fps, timestamp, vessels, source_type, observation_context)
            except Exception:
                # Avoid leaking an RTSP URL or filesystem internals into the UI.
                self.fail("录像写入失败，请检查磁盘空间和视频编码器")

    def _process_frame(self, image, camera_id, session_id, source_name, fps, timestamp, vessels, source_type, observation_context):
        if not math.isfinite(timestamp) or image is None or image.ndim != 3:
            raise ValueError("invalid source frame")
        if self.session_id != session_id:
            self._reset_optical_source()
            self._finish("source_changed")
            self._clear_buffer()
            self.hits, self.last_seen, self.last_timestamp = 0, None, None
            self.first_seen = None
            self.vessels = []
            self.session_id, self.camera_id, self.source_name = session_id, camera_id, source_name
            self.error = None
        if source_type == "camera" and (self.settings.recording_mode == "optical_cycle" or self._optical_session):
            self.last_timestamp = timestamp
            self._maintenance()
            fps = min(120., max(1., fps if math.isfinite(fps) else 25.))
            self._process_optical_frame(image, fps, timestamp, vessels, observation_context)
            return
        if not self.settings.enabled or self.error:
            return
        if self.last_timestamp is not None and (timestamp < self.last_timestamp or timestamp - self.last_timestamp > 5):
            self._finish("source_discontinuity")
            self._clear_buffer()
            self.hits, self.last_seen = 0, None
            self.first_seen = None
            self.vessels = []
        self.last_timestamp = timestamp
        self._maintenance()
        if self.error:
            return
        fps = min(120., max(1., fps if math.isfinite(fps) else 25.))
        if not self.active and self.last_seen is not None and timestamp - self.last_seen > self.settings.post_roll_seconds:
            self.hits = 0
            self.first_seen = None
        if vessels is not None:
            qualified = [v for v in vessels if v.get("processing", {}).get("eligible") is not False
                and float(v.get("confidence", 0)) >= self.settings.confidence_threshold and any(b.get("type") == "ship" for b in v.get("boxes", []))]
            if qualified:
                if not self.hits:
                    self.first_seen = timestamp
                self.hits += 1
                self.last_seen = timestamp
                self.vessels = qualified
            else:
                self.hits = 0
                self.first_seen = None
        visible = self.vessels if self.last_seen is not None and timestamp - self.last_seen <= self.settings.post_roll_seconds else []
        focus_settings = self.settings.model_copy(update={"zoom_factor": min(8., self.settings.zoom_factor)})
        self.focus = focus_rectangle(image.shape[1], image.shape[0], visible, focus_settings)
        # End at the last accepted post-roll frame before a later detection.
        if self.active and self.last_seen is not None and timestamp - self.last_seen > self.settings.post_roll_seconds:
            self._finish("vessel_left")
            self.hits = 0
            self.first_seen = None
            self.vessels = []
        if self.active and (timestamp - self.active["start"] >= self.settings.max_clip_seconds or image.shape[:2] != self.active["shape"]):
            self._finish("max_duration" if image.shape[:2] == self.active["shape"] else "resolution_changed")
        if not self.active:
            self._buffer_frame(timestamp, image)
            if self.hits < self.settings.min_hits or not visible:
                return
            self._start(image, fps, timestamp)
            buffered = list(self.buffer)
            self._clear_buffer()
            for sample_timestamp, encoded in buffered:
                decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                if decoded is not None:
                    self._write_frame(decoded, sample_timestamp)
            if not buffered:
                self._write_frame(image, timestamp)
        else:
            self._write_frame(image, timestamp)
        if self.active:
            item = self.catalog[self.active["id"]]
            for vessel in visible:
                name = vessel.get("ship_name")
                track_id = vessel.get("ship_id")
                if name and name != "UNKNOWN" and name not in item["ship_names"]:
                    item["ship_names"].append(str(name))
                if isinstance(track_id, int) and track_id not in item["track_ids"]:
                    item["track_ids"].append(track_id)

    def _start(self, image: np.ndarray, fps: float, timestamp: float, single: bool = False):
        recording_id = uuid.uuid4().hex
        directory = self.root / recording_id
        directory.mkdir()
        start = self.buffer[0][0] if self.buffer else timestamp
        started_at = datetime.now(timezone.utc) - timedelta(seconds=timestamp - start)
        saved_settings = self.settings.model_dump() if single else self.settings.model_copy(update={"recording_mode": "passage", "zoom_factor": min(8., self.settings.zoom_factor)}).model_dump()
        item = {"id": recording_id, "camera_id": self.camera_id, "source_name": self.source_name, "session_id": self.session_id,
                "started_at": started_at.isoformat(), "ended_at": None, "duration_seconds": 0., "status": "recording", "reason": None,
                "ship_names": [], "track_ids": [], "bytes": 0, "has_focus": not single, "settings": saved_settings, "recording_mode": "optical_cycle" if single else "passage",
                "source_fps": fps, "source_width": int(image.shape[1]), "source_height": int(image.shape[0]), "error": None}
        item["source_start_seconds"] = start
        item["source_end_seconds"] = start
        self.catalog[recording_id] = item
        _atomic_json(directory / "manifest.json", item)
        try:
            writer = ClipWriter(directory, image, fps, single=True) if single else ClipWriter(directory, image, fps)
        except Exception:
            item.update(status="failed", reason="writer_failed", error="无法创建视频编码器")
            _atomic_json(directory / "manifest.json", item)
            raise
        self.active = {"id": recording_id, "writer": writer, "start": start, "fps": fps, "frames": 0, "shape": image.shape[:2], "previous": None, "previous_focus": None, "persisted_at": time.monotonic()}
        record_event("recording_started", recording_id=recording_id, camera_id=self.camera_id,
            session_id=self.session_id, recording_mode=item["recording_mode"])
        if not cv2.imwrite(str(directory / "thumbnail.jpg"), cv2.resize(image, (480, max(2, round(image.shape[0] * 480 / image.shape[1]))))):
            raise RuntimeError("cannot save thumbnail")

    def _write_frame(self, image: np.ndarray, timestamp: float):
        clip = self.active
        if not clip:
            return
        target_frames = round((timestamp - clip["start"]) * clip["fps"]) + 1
        while clip["frames"] < target_frames - 1 and clip["previous"] is not None:
            clip["writer"].write(clip["previous"], clip["previous_focus"])
            clip["frames"] += 1
        if clip["frames"] < target_frames:
            clip["writer"].write(image, self.focus)
            clip["frames"] += 1
        clip["previous"] = image.copy()
        clip["previous_focus"] = dict(self.focus)
        self.catalog[clip["id"]]["duration_seconds"] = round(clip["frames"] / clip["fps"], 3)
        self.catalog[clip["id"]]["source_end_seconds"] = timestamp
        if time.monotonic() - clip["persisted_at"] >= 2:
            _atomic_json(self.root / clip["id"] / "manifest.json", self.catalog[clip["id"]])
            clip["persisted_at"] = time.monotonic()

    def _finish(self, reason: str):
        clip, self.active = self.active, None
        if not clip:
            return
        clip["writer"].close()
        item = self.catalog[clip["id"]]
        ended = datetime.fromisoformat(item["started_at"]) + timedelta(seconds=item["duration_seconds"])
        item.update(status="processing", reason=reason, ended_at=ended.isoformat())
        record_event("recording_encoding", recording_id=item["id"], camera_id=self.camera_id,
            session_id=self.session_id, recording_mode=item["recording_mode"], reason=reason)
        _atomic_json(self.root / item["id"] / "manifest.json", item)
        interrupted = reason in {"source_error", "source_discontinuity", "storage_limit", "process_restarted", "zoom_failed", "zoom_timeout", "cycle_timeout", "resolution_changed"}
        if item.get("recording_mode") == "optical_cycle" and not item.get("returned_to_overview"):
            interrupted = True
        if item.get("zoom_fallback"):
            interrupted = True
        self.executor.submit(self._encode, item["id"], "interrupted" if interrupted else "completed")

    def finish(self, reason: str = "source_stopped"):
        with self.lock:
            if self._optical_session or self._control_future is not None:
                self._halt_optical(reason)
            else:
                self._finish(reason)
            self._clear_buffer()
            self.hits, self.last_seen = 0, None
            self.first_seen = None
            self.vessels = []
            if not self.active:
                self.focus = None

    def fail(self, error: str):
        with self.lock:
            self.error = error
            record_event("recording_failed", camera_id=self.camera_id,
                session_id=self.session_id, reason="recording_error")
            if self._optical_session or self._control_future is not None:
                self._halt_optical("source_error")
            else:
                self._finish("source_error")
            self._clear_buffer()
            self.hits, self.last_seen = 0, None
            self.first_seen = None

    def _encode(self, recording_id: str, final_status: str):
        directory = self.root / recording_id
        error = None
        try:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("ffmpeg unavailable")
            kinds = ("cycle",) if self.catalog[recording_id].get("recording_mode") == "optical_cycle" else ("original", "focused")
            for kind in kinds:
                raw = directory / f"{kind}.capture.mp4"
                target = directory / f"{kind}.mp4"
                if target.is_file() and not raw.is_file():
                    continue
                temporary = directory / f"{kind}.encoding.mp4"
                result = subprocess.run([ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-i", str(raw), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)], capture_output=True, timeout=600, check=False)
                if result.returncode or not temporary.is_file() or temporary.stat().st_size < 100:
                    raise RuntimeError("video encoding failed")
                os.replace(temporary, target)
                raw.unlink(missing_ok=True)
        except Exception:
            error = "录像编码未完成；请检查 ffmpeg、磁盘空间或源文件完整性"
        with self.lock:
            item = self.catalog.get(recording_id)
            if item is None:
                return
            item.update(status="failed" if error else final_status, error=error)
            item["bytes"] = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
            _atomic_json(directory / "manifest.json", item)
            record_event("recording_failed" if error else "recording_finished",
                recording_id=recording_id, camera_id=item.get("camera_id"),
                session_id=item.get("session_id"), recording_mode=item.get("recording_mode"),
                reason="encoding_failed" if error else item.get("reason"))
            self._maintenance(force=True)

    def _storage_bytes(self) -> int:
        total = 0
        for path in self.root.glob("*/*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                # An encoder can atomically replace/remove its temporary file.
                continue
        return total

    def _maintenance(self, force: bool = False):
        if not force and time.monotonic() - self.last_maintenance < 5:
            return
        self.last_maintenance = time.monotonic()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.retention_days)
        completed = sorted((i for i in self.catalog.values() if i["status"] not in {"recording", "processing"}), key=lambda i: i["started_at"])
        for item in completed:
            if datetime.fromisoformat(item["started_at"]) < cutoff:
                try:
                    self._delete(item["id"])
                except (ValueError, OSError):
                    continue
        quota = int(self.settings.max_storage_gb * 1024**3)
        total = self._storage_bytes()
        for item in completed:
            if total <= quota:
                break
            if item["id"] in self.catalog:
                try:
                    self._delete(item["id"])
                except (ValueError, OSError):
                    continue
                total = self._storage_bytes()
        if self.active and (total > quota or shutil.disk_usage(self.root).free < 128 * 1024**2):
            if self._optical_session:
                self._halt_optical("storage_limit")
            else:
                self._finish("storage_limit")
            self.error = "录像存储空间不足，已停止录制；请释放空间或调整配额后保存设置"
            self._clear_buffer()

    def status(self) -> dict:
        with self.lock:
            self._check_zoom_timeout()
            self._maintenance()
            return {"state": "error" if self.error else "disabled" if not self.settings.enabled else "recording" if self.active else "armed",
                    "active_id": self.active["id"] if self.active else None,
                    "buffer_seconds": round(self.buffer[-1][0] - self.buffer[0][0], 2) if len(self.buffer) > 1 else 0,
                    "error": self.error, "source_name": self.source_name, "storage_bytes": self._storage_bytes(), "zoom_capability": "optical" if self._optical_session else "digital", "focus": self.focus,
                    "phase": self.phase, "current_zoom_factor": self.current_zoom_factor, "target_zoom_factor": self._active_zoom_factor if self.active else self.settings.zoom_factor,
                    "cycle_count": self.cycle_count, "zoom_driver": "optical" if self._optical_session else "digital",
                    "awaiting_zoomed_target": self._optical_session and self.phase == "zoomed" and not self._zoomed_ship_seen,
                    "zoom_recovery": {"active": self.phase == "recovering", "failure_kind": self._zoom_failure_kind,
                        "attempts": self._zoom_recovery_attempts, "fallback_recording": bool(self.active and self._zoom_fallback_active),
                        "retry_in_seconds": max(0., self._zoom_recovery_due - time.monotonic()) if self._zoom_recovery_due is not None else None},
                    "adaptive_selection": self.adaptive_selection, "locked_target": self.locked_target}

    def _public_item(self, item: dict) -> dict:
        value = dict(item)
        directory = self.root / item["id"]
        playable = item["status"] in {"completed", "interrupted"}
        for kind, suffix in (("original", "mp4"), ("focused", "mp4"), ("thumbnail", "jpg")):
            value[f"{kind}_url"] = f"/api/recordings/{item['id']}/{kind}" if (kind == "thumbnail" or playable) and (directory / f"{kind}.{suffix}").is_file() else None
        value["cycle_url"] = f"/api/recordings/{item['id']}/cycle" if playable and item.get("recording_mode") == "optical_cycle" and (directory / "cycle.mp4").is_file() else None
        if item.get("recording_mode") == "optical_cycle":
            value["original_url"] = value["cycle_url"]
            value["focused_url"] = None
        return value

    def list_recordings(self, query: str = "", page: int = 1, page_size: int = 12, camera_id: str = "", date_from: datetime | None = None, date_to: datetime | None = None) -> dict:
        with self.lock:
            items = list(self.catalog.values())
            if query.strip():
                q = query.strip().casefold()
                items = [i for i in items if q in " ".join([i["source_name"] or "", i["camera_id"] or "", *i["ship_names"], *(str(t) for t in i["track_ids"])]).casefold()]
            if camera_id:
                items = [i for i in items if i["camera_id"] == camera_id]
            if date_from:
                items = [i for i in items if datetime.fromisoformat(i["started_at"]) >= date_from]
            if date_to:
                items = [i for i in items if datetime.fromisoformat(i["started_at"]) <= date_to]
            items.sort(key=lambda i: i["started_at"], reverse=True)
            return {"items": [self._public_item(i) for i in items[(page - 1) * page_size:page * page_size]], "total": len(items), "page": page, "page_size": page_size}

    def _delete(self, recording_id: str):
        # IDs come only from the validated in-memory catalog, never a raw path.
        target = (self.root / recording_id).resolve()
        if target.parent != self.root or not re.fullmatch(r"[a-f0-9]{32}", recording_id):
            raise ValueError("invalid recording id")
        if target.exists():
            try:
                shutil.rmtree(target)
            except OSError as exc:
                # Windows can hold a video open during playback/download.
                # Preserve the index if rmtree removed the manifest first.
                if target.is_dir() and recording_id in self.catalog:
                    _atomic_json(target / "manifest.json", self.catalog[recording_id])
                raise ValueError("录像文件正在使用或无法删除，请关闭播放/下载后重试") from exc
        self.catalog.pop(recording_id, None)

    def delete_recording(self, recording_id: str):
        with self.lock:
            if recording_id not in self.catalog:
                raise FileNotFoundError(recording_id)
            if self.catalog[recording_id]["status"] in {"recording", "processing"}:
                raise ValueError("正在录制或编码的片段不能删除")
            self._delete(recording_id)

    def media_path(self, recording_id: str, kind: str) -> Path:
        with self.lock:
            if recording_id not in self.catalog or kind not in {"original", "focused", "thumbnail", "cycle"}:
                raise FileNotFoundError(recording_id)
            if kind != "thumbnail" and self.catalog[recording_id]["status"] not in {"completed", "interrupted"}:
                raise FileNotFoundError(recording_id)
            if kind == "original" and self.catalog[recording_id].get("recording_mode") == "optical_cycle":
                kind = "cycle"
            path = (self.root / recording_id / f"{kind}.{'jpg' if kind == 'thumbnail' else 'mp4'}").resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise FileNotFoundError(recording_id)
            return path


recording_manager = RecordingManager(get_settings().result_root / "passages")
