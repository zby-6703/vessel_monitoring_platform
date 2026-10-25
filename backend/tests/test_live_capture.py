import asyncio
from dataclasses import replace
import time

import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest

from app import api, live_capture
from app.config import get_settings
from app.main import app
from app.schemas import FrameResultV2


class FakeModels:
    def __init__(self, settings):
        self.settings = settings

    def load(self):
        pass

    def release(self):
        pass


class EmptyInference:
    def __init__(self, _models):
        self.calls = 0

    def infer(self, image, **kwargs):
        self.calls += 1
        return FrameResultV2(task_id=kwargs["task_id"], source_filename=kwargs["source_filename"],
            sample_id=f"frame-{kwargs['frame_index']}", frame_index=kwargs["frame_index"],
            source_frame_index=kwargs["source_frame_index"], observed_at=kwargs["observed_at"],
            image_width=image.shape[1], image_height=image.shape[0], processing_ms=0)


class NullRepository:
    def __init__(self, *_args):
        pass

    def save_realtime_frame(self, *_args):
        pass


class FrameRecorder:
    def __init__(self):
        self.frames = []
        self.finishes = []

    def process_frame(self, image, **kwargs):
        self.frames.append(kwargs)

    def get_settings(self):
        return {"enabled": False}

    def finish(self, reason):
        self.finishes.append(reason)

    def fail(self, error):
        raise AssertionError(f"unexpected capture failure: {error}")


def test_local_video_records_skipped_inference_frames_at_source_fps(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 6, (64, 48))
    assert writer.isOpened()
    for index in range(12):
        writer.write(np.full((48, 64, 3), index * 10, dtype=np.uint8))
    writer.release()
    settings = replace(get_settings(), result_root=tmp_path / "results")
    recorder = FrameRecorder()
    monkeypatch.setattr(api, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(api, "ModelRegistry", FakeModels)
    monkeypatch.setattr(api, "FrameInferenceService", EmptyInference)
    monkeypatch.setattr(api, "ResultRepository", NullRepository)
    monkeypatch.setattr(api, "recording_manager", recorder)
    worker = api._LocalPlayback()

    async def run():
        worker.start(source, "test-camera", 4, asyncio.get_running_loop(), source_name="original.mp4")
        await asyncio.to_thread(worker.join)

    asyncio.run(run())
    assert worker.snapshot()["status"] == "completed"
    assert len(recorder.frames) == 12
    assert [item["timestamp"] for item in recorder.frames] == [index / 6 for index in range(12)]
    assert all(item["fps"] == 6 and item["source_name"] == "original.mp4" for item in recorder.frames)
    assert [index for index, item in enumerate(recorder.frames) if item["vessels"] is not None] == [0, 4, 8]
    assert recorder.finishes == ["source_completed"]


def test_camera_capture_does_not_wait_for_slow_inference(monkeypatch, tmp_path):
    settings = replace(get_settings(), result_root=tmp_path / "results", live_capture_url="rtsp://user:private@example/stream")
    recorder = FrameRecorder()
    inference_calls = []

    class SlowInference(EmptyInference):
        def infer(self, image, **kwargs):
            inference_calls.append(kwargs["frame_index"])
            time.sleep(.15)
            return super().infer(image, **kwargs)

    async def broadcast(_message):
        pass

    worker = live_capture.CameraCapture(broadcast, api._live_frame)

    class Camera:
        def __init__(self):
            self.index = 0

        def open(self, *_args):
            return True

        def get(self, _property):
            return 12

        def read(self):
            if self.index == 30:
                worker.stop_event.set()
                return False, None
            time.sleep(.002)
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(live_capture, "ModelRegistry", FakeModels)
    monkeypatch.setattr(live_capture, "FrameInferenceService", SlowInference)
    monkeypatch.setattr(live_capture, "ResultRepository", NullRepository)
    monkeypatch.setattr(live_capture.cv2, "VideoCapture", Camera)
    monkeypatch.setattr(live_capture, "recording_manager", recorder)

    async def run():
        worker.start(asyncio.get_running_loop(), frame_step=1)
        await asyncio.to_thread(worker.join)

    asyncio.run(run())
    assert len(recorder.frames) == 30
    assert 1 <= len(inference_calls) < 30
    assert all(item["fps"] == 12 for item in recorder.frames)
    assert worker.snapshot()["status"] == "stopped"
    assert settings.live_capture_url not in str(worker.snapshot())


def test_live_sources_are_exclusive_and_unconfigured_hardware_is_honest(monkeypatch):
    settings = replace(get_settings(), live_capture_url="", camera_onvif_ptz_url="", camera_onvif_profile_token="")
    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: settings)
    with TestClient(app) as client:
        assert client.get("/api/realtime/camera").json()["configured"] is False
        assert client.post("/api/realtime/camera/start").status_code == 422
        assert client.get("/api/camera/ptz").json()["supported"] is False
        assert client.post("/api/camera/ptz", json={"action": "zoom_in"}).status_code == 409
        monkeypatch.setattr(api.local_playback, "is_alive", lambda: True)
        assert client.post("/api/realtime/camera/start").status_code == 409
        monkeypatch.setattr(api.local_playback, "is_alive", lambda: False)
        monkeypatch.setattr(api.camera_capture, "is_alive", lambda: True)
        response = client.post("/api/realtime/local-video", files={"file": ("test.mp4", b"video", "video/mp4")})
        assert response.status_code == 409


def test_camera_error_redacts_credentials():
    settings = replace(get_settings(), live_capture_url="rtsp://user:secret@host/stream", camera_onvif_password="lens-password")
    message = live_capture.safe_camera_error(f"cannot connect {settings.live_capture_url}; lens-password; https://user:other@host/onvif", settings)
    assert "secret" not in message
    assert "lens-password" not in message
    assert "other" not in message
    assert "user:" not in message


@pytest.mark.parametrize("error", ["ship_detector_failed: invalid model output", "frame_inference_failed: broken tensor"])
def test_detector_error_is_not_treated_as_an_empty_scene(error):
    from types import SimpleNamespace
    with pytest.raises(RuntimeError, match="船舶检测失败"):
        live_capture.ensure_detection_available(SimpleNamespace(errors=[error]))
    live_capture.ensure_detection_available(SimpleNamespace(errors=["ship_name_ocr_failed: unreadable", "draft_estimation_failed: unreadable"]))


def test_recording_sampling_and_runtime_latency_are_validated(monkeypatch):
    monkeypatch.setattr(live_capture.recording_manager, "get_settings", lambda: {"enabled": True, "post_roll_seconds": 5})
    with pytest.raises(RuntimeError, match="采样间隔"):
        live_capture.validate_recording_interval(1000, 25)
    with pytest.raises(RuntimeError, match="推理结果间隔/耗时"):
        live_capture.validate_recording_interval(3, 25, inference_seconds=6)
    live_capture.validate_recording_interval(3, 25, inference_seconds=.1)
