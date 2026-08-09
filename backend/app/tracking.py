"""Video-only instance accumulation and deterministic final fusion."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
import sys

import numpy as np

from .config import PLATFORM_ROOT
from .schemas import AttributeStatus, InstanceResultV2, InstanceState, VesselFrameResult, assert_transition, ALLOWED_INSTANCE_TRANSITIONS, recognized_name_fields

if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from runtime_support.algorithms.structure_constrained_vessel_draft_depth_estimation import StructureConstrainedVesselDraftDepthEstimation
from runtime_support.algorithms.tracktrack import TrackTrackTracker, detections_from_arrays


def _weighted_median(values: list[tuple[float, float]]) -> float:
    ordered = sorted(values, key=lambda item: item[0])
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return float(median(value for value, _ in ordered))
    threshold = total / 2
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= threshold:
            return value
    return ordered[-1][0]


def fuse_name(candidates: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in candidates:
        if item.get("key"):
            grouped[item["key"]].append(item)
    if not grouped:
        return {"recognized_zh": "UNKNOWN", "recognized_en": "UNKNOWN", "share": 0.0, "status": AttributeStatus.PENDING_REVIEW, "candidates": []}
    ranked = sorted(
        ((key, sum(value["weight"] for value in items), len(items), max(value["seen_at"] for value in items), items) for key, items in grouped.items()),
        key=lambda value: (-value[1], -value[2], -value[3].timestamp(), value[0]),
    )
    key, score, count, _, items = ranked[0]
    total = sum(value[1] for value in ranked)
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    share = score / total if total else 0.0
    status = AttributeStatus.CONFIRMED if count >= 2 and share >= .70 and (score - runner_up) / total >= .15 else AttributeStatus.PENDING_REVIEW
    zh, en = recognized_name_fields(items[-1]["text"])
    return {
        "recognized_zh": zh,
        "recognized_en": en,
        "share": round(share, 4),
        "status": status,
        "candidates": [{"value": value[0], "weight": round(value[1], 4), "count": value[2]} for value in ranked],
    }


def fuse_draft(candidates: list[dict]) -> dict:
    values = [(float(item["depth_m"]), float(item["weight"])) for item in candidates if .1 <= float(item["depth_m"]) <= 5]
    if not values:
        return {"draft_depth_m": None, "status": AttributeStatus.PENDING_REVIEW, "statistics": {"count": 0, "rejected_count": 0, "std": None}}
    raw = np.asarray([value for value, _ in values], dtype=float)
    kept = values
    if len(raw) >= 3:
        center = float(median(raw.tolist()))
        mad = float(median(np.abs(raw - center).tolist()))
        tolerance = max(.10, 3 * mad)
        kept = [(value, weight) for value, weight in values if abs(value - center) <= tolerance]
    kept_raw = np.asarray([value for value, _ in kept], dtype=float)
    spread = float(np.std(kept_raw)) if len(kept_raw) > 1 else 0.0
    return {
        "draft_depth_m": round(_weighted_median(kept), 4),
        "status": AttributeStatus.CONFIRMED if len(kept) >= 5 and spread <= .15 else AttributeStatus.PENDING_REVIEW,
        "statistics": {"count": len(kept), "rejected_count": len(values) - len(kept), "std": round(spread, 4)},
    }


@dataclass
class _Instance:
    track_id: int
    started_at: datetime
    last_seen_at: datetime
    hits: int = 0
    state: InstanceState = InstanceState.TENTATIVE
    names: list[dict] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)


class InstanceAggregator:
    def __init__(self, task_id: str, source_filename: str, confirm_hits: int = 3, lost_seconds: float = 2, finish_seconds: float = 8) -> None:
        self.task_id = task_id
        self.source_filename = source_filename
        self.confirm_hits = confirm_hits
        self.lost_seconds = lost_seconds
        self.finish_seconds = finish_seconds
        self.instances: dict[int, _Instance] = {}
        self._finished: set[int] = set()

    def update(self, track_id: int, vessel: VesselFrameResult, observed_at: datetime) -> dict:
        instance = self.instances.get(track_id)
        if instance is None:
            instance = self.instances[track_id] = _Instance(track_id=track_id, started_at=observed_at, last_seen_at=observed_at)
        if instance.state is InstanceState.LOST:
            assert_transition(instance.state, InstanceState.CONFIRMED, ALLOWED_INSTANCE_TRANSITIONS)
            instance.state = InstanceState.CONFIRMED
        instance.hits += 1
        instance.last_seen_at = observed_at
        if instance.state is InstanceState.TENTATIVE and instance.hits >= self.confirm_hits:
            assert_transition(instance.state, InstanceState.CONFIRMED, ALLOWED_INSTANCE_TRANSITIONS)
            instance.state = InstanceState.CONFIRMED
        if vessel.name_observation and vessel.name_observation.normalized:
            item = vessel.name_observation
            instance.names.append({"key": item.normalized, "text": item.text, "weight": item.roi_confidence * item.ocr_confidence, "seen_at": observed_at})
        if vessel.draft.success and vessel.draft.depth_m is not None and any(region.label == "waterline_area" for region in vessel.regions):
            instance.drafts.append({"depth_m": vessel.draft.depth_m, "weight": vessel.draft.roi_confidence * vessel.draft.character_confidence})
        return self.current(track_id)

    def sweep(self, active_track_ids: set[int], observed_at: datetime) -> list[InstanceResultV2]:
        results = []
        for track_id, instance in list(self.instances.items()):
            if track_id in active_track_ids or track_id in self._finished:
                continue
            elapsed = (observed_at - instance.last_seen_at).total_seconds()
            if elapsed >= self.finish_seconds:
                results.append(self.finish(track_id, observed_at))
            elif elapsed >= self.lost_seconds and instance.state is not InstanceState.LOST:
                assert_transition(instance.state, InstanceState.LOST, ALLOWED_INSTANCE_TRANSITIONS)
                instance.state = InstanceState.LOST
        return results

    def current(self, track_id: int) -> dict:
        instance = self.instances[track_id]
        name = fuse_name(instance.names)
        draft = fuse_draft(instance.drafts)
        return {"current_name": name["recognized_zh"], "current_name_share": name["share"], "current_draft_depth_m": draft["draft_depth_m"], "current_status": AttributeStatus.CONFIRMED if name["status"] is AttributeStatus.CONFIRMED and draft["status"] is AttributeStatus.CONFIRMED else AttributeStatus.PENDING_REVIEW}

    def snapshot(self, track_id: int, observed_at: datetime) -> InstanceResultV2:
        """Return the current aggregate without closing the active vessel instance."""
        instance = self.instances[track_id]
        name = fuse_name(instance.names)
        draft = fuse_draft(instance.drafts)
        status = AttributeStatus.CONFIRMED if name["status"] is AttributeStatus.CONFIRMED and draft["status"] is AttributeStatus.CONFIRMED else AttributeStatus.PENDING_REVIEW
        return InstanceResultV2(task_id=self.task_id, source_filename=self.source_filename, instance_id=track_id, start_time=instance.started_at, end_time=observed_at, recognized_zh=name["recognized_zh"], recognized_en=name["recognized_en"], draft_depth_m=draft["draft_depth_m"], status=status, name_candidates=name["candidates"], draft_statistics=draft["statistics"])

    def finish(self, track_id: int, observed_at: datetime) -> InstanceResultV2:
        if track_id in self._finished:
            raise ValueError(f"instance {track_id} already finished")
        instance = self.instances[track_id]
        if instance.state is not InstanceState.FINISHED:
            assert_transition(instance.state, InstanceState.FINISHED, ALLOWED_INSTANCE_TRANSITIONS)
            instance.state = InstanceState.FINISHED
        self._finished.add(track_id)
        return self.snapshot(track_id, observed_at)

    def finish_all(self, observed_at: datetime) -> list[InstanceResultV2]:
        return [self.finish(track_id, observed_at) for track_id in self.instances if track_id not in self._finished]
