import asyncio
from dataclasses import replace
import time
import threading

import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest

from app import api, live_capture
from app.config import get_settings
from app.main import app
from app.schemas import FrameResultV2, TaskKind


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

    def set_zoom_controller(self, _controller):
        pass

    def optical_control_busy(self):
        return False

    def observe_zoom_factor(self, _factor, _generation=None):
        pass

    def zoom_observation_context(self):
        return {"generation": 0, "phase": "overview", "accept_detections": True, "zoom_factor": 1.}

    def finish(self, reason):
        self.finishes.append(reason)

    def fail(self, error):
        raise AssertionError(f"unexpected capture failure: {error}")


def test_vfr_replay_uses_decoded_pts_and_only_falls_back_when_clock_is_unusable():
    assert api._video_timestamp(1040, 12, 24.5, .43) == pytest.approx(1.04)
    assert api._video_timestamp(1050, 13, 24.5, 1.04) == pytest.approx(1.05)
    assert api._video_timestamp(float("nan"), 14, 25, 1.05) == pytest.approx(1.09)
    assert api._video_timestamp(0, 15, 25, 1.09) == pytest.approx(1.13)


def test_local_video_records_skipped_inference_frames_at_source_fps(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 6, (64, 48))
    assert writer.isOpened()
    for index in range(12):
        writer.write(np.full((48, 64, 3), index * 10, dtype=np.uint8))
    writer.release()
    settings = replace(get_settings(), result_root=tmp_path / "results")
    recorder = FrameRecorder()
    inference_tasks = []

    class DetectionOnlyInference(EmptyInference):
        def infer(self, image, **kwargs):
            inference_tasks.append(kwargs["task"])
            return super().infer(image, **kwargs)

    monkeypatch.setattr(api, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(api, "ModelRegistry", FakeModels)
    monkeypatch.setattr(api, "FrameInferenceService", DetectionOnlyInference)
    monkeypatch.setattr(api, "ResultRepository", NullRepository)
    monkeypatch.setattr(api, "recording_manager", recorder)
    worker = api._LocalPlayback()

    async def run():
        worker.start(source, "test-camera", 4, asyncio.get_running_loop(), source_name="original.mp4")
        await asyncio.to_thread(worker.join)

    asyncio.run(run())
    assert worker.snapshot()["status"] == "completed"
    assert len(recorder.frames) == 12
    assert [item["timestamp"] for item in recorder.frames] == pytest.approx([index / 6 for index in range(12)])
    assert all(item["fps"] == 6 and item["source_name"] == "original.mp4" for item in recorder.frames)
    assert [index for index, item in enumerate(recorder.frames) if item["vessels"] is not None] == [0, 4, 8]
    assert inference_tasks == [TaskKind.REGION_DETECTION] * 3
    assert recorder.finishes == ["source_completed"]


@pytest.mark.parametrize("reported_fps,override_fps,expected_fps", [(12, 0, 12), (100, 25, 25), (float("nan"), 0, 25)])
def test_camera_capture_does_not_wait_for_slow_inference(monkeypatch, tmp_path, reported_fps, override_fps, expected_fps):
    settings = replace(get_settings(), result_root=tmp_path / "results", live_capture_url="rtsp://user:private@example/stream",
                       live_capture_fps_override=override_fps, live_capture_fps=25)
    recorder = FrameRecorder()
    inference_calls = []
    inference_tasks = []

    class SlowInference(EmptyInference):
        def infer(self, image, **kwargs):
            inference_calls.append(kwargs["frame_index"])
            inference_tasks.append(kwargs["task"])
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
            return reported_fps

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
    assert inference_tasks == [TaskKind.REGION_DETECTION] * len(inference_calls)
    assert all(item["fps"] == expected_fps for item in recorder.frames)
    assert worker.snapshot()["status"] == "stopped"
    assert settings.live_capture_url not in str(worker.snapshot())


def test_camera_pairs_detection_with_context_from_before_capture_and_lens_transition(monkeypatch, tmp_path):
    settings = replace(get_settings(), result_root=tmp_path / "results", live_capture_url="rtsp://camera/stream")
    first_inferred = threading.Event()
    contexts = []

    class Recorder(FrameRecorder):
        generation = 0

        def zoom_observation_context(self):
            return {"generation": self.generation, "phase": "overview", "accept_detections": True, "zoom_factor": 1.}

        def process_frame(self, image, **kwargs):
            super().process_frame(image, **kwargs)
            self.generation += 1

    recorder = Recorder()

    async def broadcast(_message):
        pass

    worker = live_capture.CameraCapture(broadcast, api._live_frame)

    class Camera:
        index = 0
        def open(self, *_): return True
        def get(self, *_): return 10
        def release(self): pass
        def read(self):
            self.index += 1
            if self.index == 2:
                assert first_inferred.wait(1)
            elif self.index > 2:
                worker.stop_event.set()
                return False, None
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

    def infer(_service, _settings, _session, frames, detections, _loop):
        _, _, _, context = frames.get(timeout=1)
        contexts.append(context)
        detections.put_nowait(([], context))
        first_inferred.set()
        worker.stop_event.wait(1)

    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(live_capture, "ModelRegistry", FakeModels)
    monkeypatch.setattr(live_capture, "FrameInferenceService", EmptyInference)
    monkeypatch.setattr(live_capture.cv2, "VideoCapture", Camera)
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(worker, "_infer_loop", infer)

    async def run():
        worker.start(asyncio.get_running_loop(), frame_step=1)
        await asyncio.to_thread(worker.join)

    asyncio.run(run())
    assert contexts[0]["generation"] == 0
    assert recorder.frames[1]["observation_context"]["generation"] == 0
    assert recorder.frames[1]["vessels"] == []
    assert all(item["source_type"] == "camera" for item in recorder.frames)


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


@pytest.mark.parametrize("modes,expected", [(["MANUAL"], False), (["AUTO", "MANUAL"], True)])
def test_autofocus_requires_the_camera_to_declare_auto_mode(monkeypatch, modes, expected):
    import xml.etree.ElementTree as ET
    settings = replace(get_settings(), camera_onvif_ptz_url="http://camera/ptz", camera_onvif_profile_token="real-profile",
                       camera_onvif_imaging_url="http://camera/imaging", camera_onvif_source_token="real-source")
    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: settings)
    controller = live_capture.OnvifControl()
    requested = []

    def request(_settings, _namespace, action, _body):
        requested.append(action)
        return ET.fromstring('<Options>' + ''.join(f'<AutoFocusModes>{mode}</AutoFocusModes>' for mode in modes) + '</Options>')

    monkeypatch.setattr(controller, "_request", request)
    assert controller._autofocus_supported(settings) is expected
    if expected:
        assert controller.command("autofocus")["accepted"] is True
        assert "SetImagingSettings" in requested
    else:
        with pytest.raises(ValueError, match="未声明 ONVIF 自动对焦"):
            controller.command("autofocus")
        assert "SetImagingSettings" not in requested


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
