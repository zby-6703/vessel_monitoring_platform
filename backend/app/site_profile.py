"""Versioned, credential-free field policy and optics for the same camera."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .recording_settings import RecordingSettings


class CameraOptics(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    zoom_calibration: dict[float, float]
    zoom_space_uri: str
    zoom_timeout_seconds: float = Field(ge=2, le=60)
    zoom_tolerance: float = Field(ge=.0001, le=.05)

    @model_validator(mode="after")
    def validate_stops(self):
        stops = self.zoom_calibration
        if 1 not in stops or len(stops) < 2 or any(not math.isfinite(k) or k < 1 or not math.isfinite(v) for k, v in stops.items()):
            raise ValueError("现场标定必须包括 1× 和至少一个有效放大档位")
        if len(set(stops.values())) != len(stops):
            raise ValueError("不同光学倍率不能共用原生位置")
        return self


class SiteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1]
    name: str = Field(min_length=1, max_length=80)
    recording: RecordingSettings
    camera_optics: CameraOptics

    def digest(self):
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@lru_cache(maxsize=8)
def _load_cached(path: str, modified: int, size: int):
    return SiteProfile.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_site_profile(path: Path) -> SiteProfile:
    path = Path(path).resolve()
    info = path.stat()
    return _load_cached(str(path), info.st_mtime_ns, info.st_size)


def _write_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sync_recording_policy(settings_path: Path, current: RecordingSettings, profile: SiteProfile, *, force=False) -> RecordingSettings:
    """Apply once per shared content version; retain local edits in between."""
    marker = settings_path.with_name("settings.shared-profile.json")
    digest = profile.digest()
    try:
        if not force and settings_path.is_file() and json.loads(marker.read_text(encoding="utf-8")).get("sha256") == digest:
            return current
    except (OSError, ValueError, AttributeError):
        pass
    if settings_path.is_file():
        _write_json(settings_path.with_name("settings.before-shared-profile.json"), current.model_dump())
    updated = profile.recording.model_copy(deep=True)
    _write_json(settings_path, updated.model_dump())
    _write_json(marker, {"name": profile.name, "schema_version": profile.schema_version, "sha256": digest})
    return updated
