from datetime import datetime, timedelta, timezone

from app.schemas import Detection, DraftObservation, NameObservation, VesselFrameResult
from app.tracking import InstanceAggregator, fuse_draft, fuse_name


def _vessel(name="海泰52", depth=2.0):
    return VesselFrameResult(vessel_id=1, ship=Detection(label="ship", xyxy=(0, 0, 2, 2), confidence=1), regions=[Detection(label="waterline_area", xyxy=(0, 0, 2, 2), confidence=.8)], name_observation=NameObservation(text=name, normalized=name, roi_confidence=.9, ocr_confidence=.9), draft=DraftObservation(depth_m=depth, success=True, roi_confidence=.8, character_confidence=.9))


def test_name_fusion_uses_weighted_repeated_candidates():
    now = datetime.now(timezone.utc)
    fused = fuse_name([{"key": "A", "text": "A", "weight": .9, "seen_at": now}, {"key": "B", "text": "B", "weight": .2, "seen_at": now}, {"key": "A", "text": "A", "weight": .9, "seen_at": now}])
    assert fused["recognized_zh"] == "A"
    assert fused["status"].value == "confirmed"


def test_draft_fusion_rejects_outlier_with_mad():
    fused = fuse_draft([{"depth_m": 2.0, "weight": 1}, {"depth_m": 2.1, "weight": 1}, {"depth_m": 4.8, "weight": 1}])
    assert fused["draft_depth_m"] in {2.0, 2.1}
    assert fused["statistics"]["rejected_count"] == 1


def test_instance_lifecycle_finishes_once_and_recalculates_results():
    now = datetime.now(timezone.utc)
    aggregator = InstanceAggregator("task", "video.mp4", confirm_hits=2)
    aggregator.update(8, _vessel(), now)
    current = aggregator.update(8, _vessel(), now + timedelta(seconds=1))
    assert current["current_name"] == "海泰52"
    completed = aggregator.finish_all(now + timedelta(seconds=2))
    assert completed[0].instance_id == 8
    assert completed[0].status.value == "pending_review"  # less than five draft values
