"""Exercise live routing with the real detector service, tracker and repository."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import queue
from types import SimpleNamespace

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import api, live_capture
from app.config import get_settings
from app.database import Base
from app.inference.frame_inference import FrameInferenceService
from app.models import OfflineVideoResult, RealtimeTrackingResult, RealtimeVideoResult
from app.recording import RecordingSettings


class CountingModels:
    class_names = ["ship", "name", "waterlineArea"]

    def __init__(self):
        self.ship_detector = self
        self.name_recognizer = self.name
        self.draft_predictor = SimpleNamespace(predict=self.draft)
        self.depth_estimator = SimpleNamespace(estimate_with_details=lambda *args: SimpleNamespace(depth=None, success=False))
        self.detector_calls = 0
        self.name_shapes = []
        self.draft_shapes = []

    def __call__(self, image):
        self.detector_calls += 1
        return {
            "boxes": np.asarray([
                [100, 300, 450, 500], [120, 320, 150, 330], [300, 450, 330, 480],
                [650, 400, 710, 420], [660, 402, 670, 407], [680, 410, 695, 415],
            ], dtype=float),
            "scores": np.asarray([.95] * 6), "labels": np.asarray([0, 1, 2, 0, 1, 2]),
        }

    def name(self, **kwargs):
        self.name_shapes.append(kwargs["img_numpy"].size)
        return [{"text": "LARGE1", "score": .95}]

    def draft(self, image):
        self.draft_shapes.append(image.shape[:2])
        return SimpleNamespace(detection=SimpleNamespace(boxes=[]),
            segmentation=SimpleNamespace(get_binary_mask=lambda **kwargs: np.zeros(image.shape[:2], dtype=np.uint8)))


def test_camera_loop_routes_small_vessel_to_counting_and_large_vessel_to_detail(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'routing.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(live_capture, "SessionLocal", sessions)
    settings = replace(get_settings(), result_root=tmp_path / "results", live_camera_name="routing-camera")
    policy = RecordingSettings(enabled=False, suppress_stationary=False)
    context = {"generation": 0, "phase": "overview", "accept_detections": True, "zoom_factor": 1.}
    recorder = SimpleNamespace(get_settings=lambda: policy.model_dump(), zoom_observation_context=lambda: context)
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    published = []
    capture = live_capture.CameraCapture(lambda *args: None, api._live_frame)
    monkeypatch.setattr(capture, "_publish", lambda payload, loop: published.append(payload["data"]["frame"]))
    models = CountingModels()
    service = FrameInferenceService(models)
    image = np.zeros((720, 1000, 3), dtype=np.uint8)
    started_at = datetime.now(timezone.utc)

    class Frames:
        index = 0

        def get(self, timeout):
            if self.index >= 8:
                capture.stop_event.set()
                raise queue.Empty
            index = self.index
            self.index += 1
            return index * 5, image, started_at + timedelta(seconds=index * .2), context

    session = {"session_id": "test-camera-routing", "camera_id": "test-camera", "fps": 25, "frame_step": 5}
    capture._infer_loop(service, settings, session, Frames(), queue.Queue(maxsize=1), None)
    assert capture._inference_error is None
    assert models.detector_calls == 8
    # Only the large vessel's 30-pixel ROIs reach either expensive model.
    assert models.name_shapes == [(30, 10), (30, 10)]
    assert models.draft_shapes == [(30, 30), (30, 30)]
    assert len(published) == 8
    assert all(len(frame["vessels"]) == 2 for frame in published)
    assert published[-1]["processing_summary"]["lightweight"] == 1

    with sessions() as db:
        frames = list(db.scalars(select(RealtimeVideoResult)))
        counted = list(db.scalars(select(RealtimeTrackingResult)))
        recognized = list(db.scalars(select(OfflineVideoResult)))
    assert len(frames) == 8
    assert len(counted) == 2
    assert sum(row.payload_json["processing"]["lightweight_only"] for row in counted) == 1
    assert len(recognized) == 1
    assert recognized[0].recognized_zh == "LARGE1"
    engine.dispose()
