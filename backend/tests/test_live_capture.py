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


@pytest.fixture(autouse=True)
def no_real_control_keepalive(monkeypatch):
    monkeypatch.setattr(live_capture.onvif_control, "keep_control_alive", lambda *_args: True)


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


class ObserverRecorder(FrameRecorder):
    def __init__(self):
        super().__init__()
        self.busy = False
        self.generation = 7
        self.observations = []

    def optical_control_busy(self):
        return self.busy

    def zoom_observation_context(self):
        return {"generation": self.generation, "zoom_factor": None}

    def observe_zoom_factor(self, factor, generation=None):
        if not self.busy and generation == self.generation:
            self.observations.append(factor)


class PollSteps:
    """Advance observer ticks without sleeping or depending on wall time."""
    def __init__(self, ticks, on_wait=None):
        self.ticks = ticks
        self.waits = []
        self.on_wait = on_wait
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.on_wait:
            self.on_wait(len(self.waits))
        self.stopped = len(self.waits) >= self.ticks
        return self.stopped


def observer_settings():
    return replace(get_settings(), camera_onvif_ptz_url="http://camera/ptz",
        camera_onvif_profile_token="profile", camera_onvif_username="observer-test",
        camera_onvif_password="observer-test-password", camera_onvif_zoom_calibration='{"1":0.1,"3":0.4}')


@pytest.mark.parametrize("first", [None, RuntimeError("temporary camera timeout")])
def test_zoom_observer_retries_unknown_or_failed_measurement_without_assuming_one_x(monkeypatch, first):
    recorder = ObserverRecorder()
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    readings = iter([first, 3.])

    def read():
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(live_capture.onvif_control, "read_magnification", read)
    worker = live_capture.CameraCapture(None, None)
    ticks = PollSteps(2)
    worker._observe_zoom(observer_settings(), ticks)
    assert recorder.observations == [None, 3.]
    assert ticks.waits == [2., 2.]


def test_zoom_observer_skips_an_owned_cycle_and_resumes_after_it_is_idle(monkeypatch):
    recorder = ObserverRecorder()
    recorder.busy = True
    calls = []
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(live_capture.onvif_control, "read_magnification", lambda: calls.append(True) or 2.)
    ticks = PollSteps(2, on_wait=lambda tick: setattr(recorder, "busy", False))
    worker = live_capture.CameraCapture(None, None)
    worker._observe_zoom(observer_settings(), ticks)
    assert calls == [True]
    assert recorder.observations == [2.]


@pytest.mark.parametrize("phase", ["overview", "zoomed"])
def test_stable_owned_view_keeps_control_connection_warm_without_overwriting_position(monkeypatch, phase):
    recorder = ObserverRecorder()
    recorder.busy = True
    context = {"generation": 7, "phase": phase, "accept_detections": True, "zoom_factor": 3.}
    recorder.zoom_observation_context = lambda: dict(context)
    calls = []
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(live_capture.onvif_control, "keep_control_alive", lambda settings: calls.append(settings) or True)
    monkeypatch.setattr(live_capture.onvif_control, "read_magnification", lambda: pytest.fail("owned lens must not be relabelled"))
    worker = live_capture.CameraCapture(None, None)
    ticks = PollSteps(3)
    worker._observe_zoom(observer_settings(), ticks)
    assert len(calls) == 3
    assert ticks.waits == [2., 2., 2.]
    assert recorder.observations == []
    assert context == {"generation": 7, "phase": phase, "accept_detections": True, "zoom_factor": 3.}


@pytest.mark.parametrize("phase", ["initializing", "zooming_in", "zooming_out", "recovering"])
def test_observer_does_not_queue_keepalive_during_a_lens_transition(monkeypatch, phase):
    recorder = ObserverRecorder()
    recorder.busy = True
    recorder.zoom_observation_context = lambda: {"generation": 8, "phase": phase, "accept_detections": False}
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(live_capture.onvif_control, "keep_control_alive", lambda *_args: pytest.fail("lens transition owns I/O"))
    monkeypatch.setattr(live_capture.onvif_control, "read_magnification", lambda: pytest.fail("lens transition owns view"))
    live_capture.CameraCapture(None, None)._observe_zoom(observer_settings(), PollSteps(2))
    assert recorder.observations == []


@pytest.mark.parametrize("warm_result", ["success", "failure", "stop"])
def test_camera_attempt_warms_control_before_opening_video_and_honors_stop(monkeypatch, tmp_path, warm_result):
    settings = replace(observer_settings(), result_root=tmp_path / "results", live_capture_url="rtsp://camera/stream")
    worker = live_capture.CameraCapture(None, None)
    recorder = FrameRecorder()
    events = []
    def warm(_settings):
        events.append("warm")
        if warm_result == "failure":
            raise TimeoutError("simulated preflight timeout")
        if warm_result == "stop":
            worker.stop()
        return True
    class Camera:
        def open(self, *_args): events.append("open"); return True
        def get(self, *_args): return 25
        def read(self): worker.stop_event.set(); return False, None
        def release(self): events.append("release")
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(live_capture, "FrameInferenceService", EmptyInference)
    monkeypatch.setattr(live_capture.onvif_control, "keep_control_alive", warm)
    monkeypatch.setattr(live_capture.cv2, "VideoCapture", Camera)
    monkeypatch.setattr(worker, "_observe_zoom", lambda *_args: None)
    monkeypatch.setattr(worker, "_infer_loop", lambda *_args: None)
    monkeypatch.setattr(worker, "_publish", lambda *_args: None)
    failure, reason = worker._run_attempt(settings, None, 3, FakeModels(settings))
    assert failure is None and reason == "source_stopped"
    if warm_result == "stop":
        assert events == ["warm"]
    else:
        assert events == ["warm", "open", "release"]


@pytest.mark.parametrize("change", ["cycle", "generation", "stop"])
def test_inflight_zoom_observation_cannot_overwrite_cycle_new_view_or_explicit_stop(monkeypatch, change):
    recorder = ObserverRecorder()
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    worker = live_capture.CameraCapture(None, None)

    def read():
        if change == "cycle":
            recorder.busy = True
        elif change == "generation":
            recorder.generation += 1
        else:
            worker.stop()
        return 3.

    monkeypatch.setattr(live_capture.onvif_control, "read_magnification", read)
    ticks = worker._zoom_observation_stop if change == "stop" else PollSteps(1)
    worker._observe_zoom(observer_settings(), ticks)
    assert recorder.observations == []
    if change == "stop":
        assert worker.stop_event.is_set()
        assert ticks.is_set()


@pytest.mark.parametrize("missing", ["camera_onvif_ptz_url", "camera_onvif_profile_token", "camera_onvif_zoom_calibration"])
def test_zoom_observer_missing_configuration_never_issues_a_network_read(monkeypatch, missing):
    recorder = ObserverRecorder()
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(live_capture.onvif_control, "read_magnification", lambda: pytest.fail("unexpected network read"))
    worker = live_capture.CameraCapture(None, None)
    worker._observe_zoom(replace(observer_settings(), **{missing: ""}), PollSteps(1))
    assert recorder.observations == [None]


def test_zoom_observer_supports_an_anonymous_but_calibrated_camera(monkeypatch):
    recorder = ObserverRecorder()
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(live_capture.onvif_control, "read_magnification", lambda: 3.)
    worker = live_capture.CameraCapture(None, None)
    worker._observe_zoom(replace(observer_settings(), camera_onvif_username="", camera_onvif_password=""), PollSteps(1))
    assert recorder.observations == [3.]


def test_camera_capture_does_not_wait_for_a_blocked_zoom_observer(monkeypatch, tmp_path):
    settings = replace(observer_settings(), result_root=tmp_path / "results", live_capture_url="rtsp://camera/stream")
    recorder = FrameRecorder()
    observer_started, release_observer = threading.Event(), threading.Event()
    audit_events = []
    worker = live_capture.CameraCapture(lambda _message: asyncio.sleep(0), api._live_frame)

    def read_zoom():
        observer_started.set()
        assert release_observer.wait(2)
        return 3.

    class Camera:
        index = 0
        def open(self, *_): return True
        def get(self, *_): return 25
        def release(self): pass
        def read(self):
            assert observer_started.wait(1)
            if self.index == 8:
                # All source frames must arrive before the native query ends.
                assert not release_observer.is_set()
                release_observer.set()
                worker.stop_event.set()
                return False, None
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(live_capture, "ModelRegistry", FakeModels)
    monkeypatch.setattr(live_capture, "FrameInferenceService", EmptyInference)
    monkeypatch.setattr(live_capture, "ResultRepository", NullRepository)
    monkeypatch.setattr(live_capture.cv2, "VideoCapture", Camera)
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(live_capture.onvif_control, "read_magnification", read_zoom)
    monkeypatch.setattr(live_capture, "record_event", lambda event, **fields: audit_events.append((event, fields)))

    async def run():
        worker.start(asyncio.get_running_loop(), frame_step=1)
        await asyncio.to_thread(worker.join)

    asyncio.run(run())
    assert len(recorder.frames) == 8
    assert worker.snapshot()["status"] == "stopped"
    assert not worker.zoom_observation_thread.is_alive()
    assert [event for event, _ in audit_events] == ["camera_start_requested", "camera_running", "camera_stopped"]
    assert audit_events[-1][1]["frame_count"] == 8
    assert audit_events[-1][1]["reason"] == "source_stopped"


class ImmediateReconnectEvent(threading.Event):
    def __init__(self, on_wait=None):
        super().__init__()
        self.waits = []
        self.on_wait = on_wait

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.on_wait:
            self.on_wait(self)
        return self.is_set()


def test_transient_read_failure_reconnects_serially_with_new_session_and_one_model_load(monkeypatch, tmp_path):
    settings = replace(get_settings(), result_root=tmp_path / "results", live_capture_url="rtsp://camera/stream")
    worker = live_capture.CameraCapture(None, api._live_frame)
    worker._status.update(camera_id="test-camera", session_id="initial-session")
    worker._shutdown_requested = ImmediateReconnectEvent()
    calls, statuses = [], []
    active = {"workers": 0, "maximum": 0}

    class Models(FakeModels):
        def load(self): calls.append("load")
        def release(self): calls.append("release-model")

    class Recorder(FrameRecorder):
        def fail(self, error): calls.append("close-failed-clip")
        def wait_for_optical_stop(self, timeout):
            calls.append("drain-optics")
            return True

    recorder = Recorder()

    class Camera:
        opened = 0
        def __init__(self):
            self.index = 0
            Camera.opened += 1
            self.attempt = Camera.opened
        def open(self, *_):
            calls.append(f"open-{self.attempt}")
            assert active["workers"] == 0
            return True
        def get(self, *_): return 25
        def release(self): calls.append(f"release-{self.attempt}")
        def read(self):
            self.index += 1
            if self.index == 1:
                return True, np.zeros((48, 64, 3), dtype=np.uint8)
            if self.attempt == 2:
                worker.stop()
            return False, None

    def infer(*_args):
        active["workers"] += 1
        active["maximum"] = max(active["maximum"], active["workers"])
        try: worker.stop_event.wait(1)
        finally: active["workers"] -= 1

    monkeypatch.setattr(live_capture, "ModelRegistry", Models)
    monkeypatch.setattr(live_capture.cv2, "VideoCapture", Camera)
    monkeypatch.setattr(live_capture, "recording_manager", recorder)
    monkeypatch.setattr(worker, "_observe_zoom", lambda *_: None)
    monkeypatch.setattr(worker, "_infer_loop", infer)
    monkeypatch.setattr(worker, "_publish", lambda payload, _: statuses.append(payload["data"].copy()))
    worker._run(settings, None, 1)
    assert Camera.opened == 2
    assert active == {"workers": 0, "maximum": 1}
    assert calls.count("load") == calls.count("release-model") == 1
    assert calls.index("release-1") < calls.index("drain-optics") < calls.index("open-2")
    assert worker._shutdown_requested.waits == [5.]
    assert len({frame["session_id"] for frame in recorder.frames}) == 2
    assert any(state["status"] == "reconnecting" for state in statuses)
    assert worker.snapshot()["status"] == "stopped"


def test_explicit_stop_during_reconnect_wait_prevents_any_further_open(monkeypatch, tmp_path):
    settings = replace(get_settings(), result_root=tmp_path / "results", live_capture_url="rtsp://camera/stream")
    worker = live_capture.CameraCapture(None, None)
    attempts = []
    worker._shutdown_requested = ImmediateReconnectEvent(lambda _: worker.stop())
    monkeypatch.setattr(live_capture, "ModelRegistry", FakeModels)
    monkeypatch.setattr(worker, "_publish", lambda *_: None)
    monkeypatch.setattr(worker, "_run_attempt", lambda *_: attempts.append(True) or ("temporary read error", "camera_read_failed"))
    monkeypatch.setattr(live_capture, "recording_manager", FrameRecorder())
    worker._run(settings, None, 1)
    assert attempts == [True]
    assert worker.snapshot()["status"] == "stopped"
    assert worker.stop_event.is_set()


def test_reconnect_backoff_is_bounded_and_inference_failure_is_terminal(monkeypatch):
    settings = get_settings()
    worker = live_capture.CameraCapture(None, None)
    worker._shutdown_requested = ImmediateReconnectEvent()
    attempts = []

    def attempt(*_):
        attempts.append(True)
        return ("temporary connection error", "camera_connection_failed") if len(attempts) <= 5 else ("model error", "inference_failed")

    monkeypatch.setattr(live_capture, "ModelRegistry", FakeModels)
    monkeypatch.setattr(worker, "_run_attempt", attempt)
    monkeypatch.setattr(worker, "_publish", lambda *_: None)
    monkeypatch.setattr(live_capture, "recording_manager", FrameRecorder())
    worker._run(settings, None, 1)
    assert len(attempts) == 6
    assert worker._shutdown_requested.waits == [5., 10., 20., 30., 30.]


def test_model_load_error_never_enters_reconnect_attempt(monkeypatch):
    worker = live_capture.CameraCapture(None, None)
    attempts = []

    class BrokenModel(FakeModels):
        def load(self): raise RuntimeError("invalid deployment")

    monkeypatch.setattr(live_capture, "ModelRegistry", BrokenModel)
    monkeypatch.setattr(worker, "_run_attempt", lambda *_: attempts.append(True))
    monkeypatch.setattr(worker, "_publish", lambda *_: None)
    worker._run(get_settings(), None, 1)
    assert attempts == []
    assert worker.snapshot()["status"] == "failed"
