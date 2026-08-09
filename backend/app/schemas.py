"""The v2 schemas shared by the batch pipeline, storage and API.

This module intentionally owns the task/status vocabulary.  No caller should
introduce a task-specific spelling or a private state transition.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import re
from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskKind(str, Enum):
    REGION_DETECTION = "region_detection"
    SHIP_NAME_RECOGNITION = "ship_name_recognition"
    DRAFT_ESTIMATION = "draft_estimation"
    FULL_PIPELINE = "full_pipeline"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InstanceState(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    LOST = "lost"
    FINISHED = "finished"


class AttributeStatus(str, Enum):
    CONFIRMED = "confirmed"
    PENDING_REVIEW = "pending_review"


ALLOWED_JOB_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.RUNNING: {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}
ALLOWED_INSTANCE_TRANSITIONS: dict[InstanceState, set[InstanceState]] = {
    InstanceState.TENTATIVE: {InstanceState.CONFIRMED, InstanceState.LOST, InstanceState.FINISHED},
    InstanceState.CONFIRMED: {InstanceState.LOST, InstanceState.FINISHED},
    InstanceState.LOST: {InstanceState.CONFIRMED, InstanceState.FINISHED},
    InstanceState.FINISHED: set(),
}


def assert_transition(current: Enum, target: Enum, transitions: dict) -> None:
    if target == current:
        return
    if target not in transitions[current]:
        raise ValueError(f"invalid state transition: {current.value} -> {target.value}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sample_id_for(source_filename: str, frame_index: int | None = None) -> str:
    """Stable source-local ID: ``filename`` for images, ``filename#frame=N`` for video."""
    return source_filename if frame_index is None else f"{source_filename}#frame={frame_index}"


def normalize_ship_name(value: str | None) -> str:
    """NFKC, no whitespace/punctuation, but never discard Chinese characters."""
    return "".join(character.upper() for character in unicodedata.normalize("NFKC", value or "") if character.isalnum())


def recognized_name_fields(value: str | None) -> tuple[str, str]:
    key = normalize_ship_name(value)
    if not key:
        return "UNKNOWN", "UNKNOWN"
    if re.fullmatch(r"[A-Z0-9]+", key):
        return key, key
    from pypinyin import Style, lazy_pinyin
    # Keep non-Chinese Latin letters/digits as-is and transliterate Chinese
    # characters to the compact uppercase representation used by CSV/AIS.
    english = "".join(lazy_pinyin(key, style=Style.NORMAL, errors=lambda item: list(item))).upper()
    return key, english or "UNKNOWN"


class Detection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Literal["ship", "ship_name_area", "waterline_area", "draft_mark"]
    xyxy: tuple[float, float, float, float]
    confidence: float = Field(ge=0, le=1)
    class_name: str | None = None

    @field_validator("xyxy")
    @classmethod
    def validate_xyxy(cls, value: tuple[float, float, float, float]):
        if value[2] < value[0] or value[3] < value[1]:
            raise ValueError("bbox coordinates must have non-negative width and height")
        return value


class WaterPolygon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Literal["water"] = "water"
    points: list[tuple[float, float]] = Field(min_length=3)
    confidence: float = Field(default=1.0, ge=0, le=1)


class NameObservation(BaseModel):
    text: str = ""
    normalized: str = ""
    roi_confidence: float = Field(default=0, ge=0, le=1)
    ocr_confidence: float = Field(default=0, ge=0, le=1)


class DraftObservation(BaseModel):
    depth_m: float | None = Field(default=None, ge=0)
    success: bool = False
    roi_confidence: float = Field(default=0, ge=0, le=1)
    character_confidence: float = Field(default=0, ge=0, le=1)
    method: str | None = None


class VesselFrameResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vessel_id: int = Field(ge=1)
    track_id: int | None = Field(default=None, ge=1)
    ship: Detection
    regions: list[Detection] = Field(default_factory=list)
    draft_marks: list[Detection] = Field(default_factory=list)
    water: list[WaterPolygon] = Field(default_factory=list)
    recognized_zh: str = "UNKNOWN"
    recognized_en: str = "UNKNOWN"
    name_observation: NameObservation | None = None
    draft: DraftObservation = Field(default_factory=DraftObservation)
    errors: list[str] = Field(default_factory=list)


class FrameResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["frame"] = "frame"
    schema_version: Literal["2.0"] = "2.0"
    task_id: str
    source_filename: str
    sample_id: str
    frame_index: int | None = Field(default=None, ge=0)
    source_frame_index: int | None = Field(default=None, ge=0)
    observed_at: datetime
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    processing_ms: float = Field(ge=0)
    vessels: list[VesselFrameResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class InstanceResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["instance"] = "instance"
    task_id: str
    source_filename: str
    instance_id: int = Field(ge=1)
    start_time: datetime
    end_time: datetime
    recognized_zh: str = "UNKNOWN"
    recognized_en: str = "UNKNOWN"
    draft_depth_m: float | None = Field(default=None, ge=0)
    status: AttributeStatus = AttributeStatus.PENDING_REVIEW
    name_candidates: list[dict] = Field(default_factory=list)
    draft_statistics: dict = Field(default_factory=dict)
