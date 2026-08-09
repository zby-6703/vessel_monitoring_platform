from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

from app.schemas import TaskKind
from app.inference.frame_inference import FrameInferenceService


class FakeDetector:
    class_names = ["ship", "name", "waterlineArea"]

    def __call__(self, frame):
        return {"boxes": np.array([[1, 1, 19, 19], [2, 2, 10, 8], [10, 10, 18, 18]], dtype=float), "scores": np.array([.9, .8, .7]), "labels": np.array([0, 1, 2])}


class FakeName:
    def __call__(self, **kwargs):
        return [{"text": " 贵港 海泰 52 ", "score": .9}]


def test_frame_service_returns_v2_result_and_keeps_chinese_name():
    models = SimpleNamespace(ship_detector=FakeDetector(), name_recognizer=FakeName())
    service = FrameInferenceService(models)
    result = service.infer(np.zeros((20, 20, 3), dtype=np.uint8), task_id="task-1", source_filename="ship.jpg", task=TaskKind.FULL_PIPELINE, observed_at=datetime.now(timezone.utc))
    assert result.schema_version == "2.0"
    assert result.vessels[0].recognized_zh == "贵港海泰52"
    assert result.vessels[0].draft.depth_m is None
    assert any("draft_estimation_failed" in value for value in result.errors)


def test_empty_detector_is_a_valid_empty_frame_result():
    class EmptyDetector:
        class_names = ["ship"]
        def __call__(self, frame): return {"boxes": np.empty((0, 4)), "scores": np.array([]), "labels": np.array([])}
    service = FrameInferenceService(SimpleNamespace(ship_detector=EmptyDetector()))
    result = service.infer(np.zeros((4, 4, 3), dtype=np.uint8), task_id="task", source_filename="video.mp4", task=TaskKind.REGION_DETECTION, observed_at=datetime.now(timezone.utc), frame_index=0, source_frame_index=4)
    assert result.vessels == []
    assert result.sample_id == "video.mp4#frame=0"


def test_water_mask_contours_are_converted_to_polygons():
    mask = np.zeros((20, 20), dtype=np.uint8)
    mask[2:10, 2:10] = 1
    mask[15:17, 15:17] = 1  # too small and therefore filtered
    polygons = FrameInferenceService._mask_polygons(mask, (10, 20, 30, 40))
    assert len(polygons) == 1
    assert all(point[0] >= 10 and point[1] >= 20 for point in polygons[0].points)
