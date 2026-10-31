"""Contract tests for physical zoom cycles, without a connected camera.

The fake lens represents acknowledged physical moves. It never converts a
requested magnification into a normalized ONVIF position on the recorder's
behalf, and can hold a move in progress to verify save/return ordering.
"""
from __future__ import annotations

from pathlib import Path
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app import recording as recording_module
from app import optical_recording as optical_module
from app import recording_api
from app.recording import RecordingManager
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeZoom:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.block_factor = None
        self.fail_factor = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def validate_cycle(self, factor):
        if factor not in {1, 3}:
            raise ValueError("指定倍率尚未标定")

    def move_to(self, factor):
        self.calls.append(factor)
        self.events.append(("move_requested", factor))
        if self.block_factor == factor:
            self.entered.set()
            assert self.release.wait(5), "test did not release fake lens"
        if self.fail_factor == factor:
            raise RuntimeError("镜头运动失败")
        self.events.append(("move_settled", factor))
        return {"factor": factor, "position": {1: 0, 3: .4}[factor], "settled": True}


class CycleHarness:
    def __init__(self, manager, controller, events):
        self.manager = manager
        self.controller = controller
        self.events = events
        self.timestamp = 0.

    def feed(self, vessels=None, *, context=None, advance=.1):
        self.timestamp += advance
        if context is None and vessels is not None:
            context = dict(self.manager.zoom_observation_context())
        if vessels is not None:
            # Synthetic detections start in the 1x plane. A physical lens
            # magnifies them and clips boxes at the current field-of-view edge.
            zoom = self.manager.current_zoom_factor or 1
            projected = []
            for vessel in vessels:
                boxes = []
                for box in vessel["boxes"]:
                    x, y, width, height = box["xywh"]
                    x1, y1 = max(0, (x / 64 - .5) * zoom + .5), max(0, (y / 48 - .5) * zoom + .5)
                    x2, y2 = min(1, ((x + width) / 64 - .5) * zoom + .5), min(1, ((y + height) / 48 - .5) * zoom + .5)
                    if x2 > x1 and y2 > y1:
                        boxes.append({**box, "xywh": [x1 * 64, y1 * 48, (x2 - x1) * 64, (y2 - y1) * 48]})
                if boxes:
                    projected.append({**vessel, "boxes": boxes})
            vessels = projected
        self.manager.process_frame(
            np.full((48, 64, 3), int(self.timestamp * 10) % 255, dtype=np.uint8),
            camera_id="optical-camera", session_id="optical-session", source_name="现场摄像头",
            source_type="camera", fps=10, timestamp=self.timestamp,
            vessels=vessels, observation_context=context,
        )
        return self.manager.status()

    def drain_control(self):
        self.manager.control_executor.submit(lambda: None).result(timeout=5)

    def phase(self, target):
        for _ in range(12):
            self.drain_control()
            status = self.feed()
            if status["phase"] == target:
                return status
        raise AssertionError(f"did not reach {target}: {self.manager.status()}")

    def warm(self):
        self.feed()
        self.phase("overview")
        # First view after the acknowledged 1x move must have its own inference.
        self.feed([])

    def start_cycle(self):
        status = self.feed([ship()])
        assert status["active_id"] is not None, status
        self.phase("zoomed")
        self.feed([ship()])
        return self.manager.status()["active_id"]

    def depart(self):
        self.feed([])
        for _ in range(10):
            status = self.feed([], advance=.1)
            if status["phase"] == "zooming_out":
                return status
        raise AssertionError(f"did not request panorama return: {self.manager.status()}")


def ship():
    return {"ship_id": 1, "ship_name": "海泰52", "confidence": .95,
            "boxes": [{"type": "ship", "xywh": [4, 8, 50, 28], "confidence": .95}]}


def test_cycle_api_streams_one_mp4_with_seek_support(cycle_factory, monkeypatch):
    harness = cycle_factory(real_video=True)
    harness.warm()
    recording_id = harness.start_cycle()
    harness.depart()
    harness.phase("overview")
    harness.manager.executor.submit(lambda: None).result(timeout=10)
    monkeypatch.setattr(recording_api, "recording_manager", harness.manager)
    app = FastAPI()
    app.include_router(recording_api.router)
    with TestClient(app) as client:
        item = client.get("/api/recordings").json()["items"][0]
        assert item["id"] == recording_id
        assert item["cycle_url"] == item["original_url"]
        assert item["focused_url"] is None
        assert item["has_focus"] is False
        response = client.get(item["cycle_url"], headers={"Range": "bytes=0-31"})
        assert response.status_code == 206
        assert len(response.content) == 32
        assert client.get(f"/api/recordings/{recording_id}/original").status_code == 200
        assert client.get(f"/api/recordings/{recording_id}/focused").status_code == 404
    assert len(list((harness.manager.root / recording_id).glob("*.mp4"))) == 1


@pytest.fixture
def cycle_factory(tmp_path, monkeypatch):
    created = []

    def create(*, real_video=False, **settings):
        events = []
        controller = FakeZoom(events)
        if not real_video:
            class SpyWriter:
                def __init__(self, directory, *_args, **_kwargs):
                    self.recording_id = Path(directory).name
                    events.append(("recording_open", self.recording_id))

                def write(self, *_args, **_kwargs):
                    events.append(("frame", self.recording_id))

                def close(self):
                    events.append(("recording_close", self.recording_id))

            monkeypatch.setattr(recording_module, "ClipWriter", SpyWriter)
        manager = RecordingManager(tmp_path / str(len(created)))
        manager.set_zoom_controller(controller)
        if not real_video:
            def finalize(recording_id, status):
                with manager.lock:
                    manager.catalog[recording_id]["status"] = status
            monkeypatch.setattr(manager, "_encode", finalize)
        manager.update_settings({
            "recording_mode": "optical_cycle", "enabled": True,
            "adaptive_zoom_enabled": False, "suppress_stationary": False,
            "zoom_factor": 3, "min_hits": 1, "pre_roll_seconds": 0,
            "departure_confirm_seconds": .3, "overview_settle_seconds": 0,
            "zoom_settle_timeout_seconds": 3, **settings,
        })
        harness = CycleHarness(manager, controller, events)
        created.append(harness)
        return harness

    yield create
    for harness in created:
        harness.controller.release.set()
        harness.manager.finish()
        harness.drain_control()
        # Closing a source must not require another decoded source frame.
        harness.manager.control_executor.shutdown(wait=True)
        harness.manager.executor.shutdown(wait=True)


@pytest.mark.parametrize("factor", [1, 3])
def test_one_transient_absolute_move_failure_is_retried_without_losing_the_cycle(cycle_factory, monkeypatch, factor):
    import httpx
    harness = cycle_factory()
    original = harness.controller.move_to
    attempts = []
    def transient(target):
        attempts.append(target)
        if target == factor and attempts.count(target) == 1:
            raise httpx.ReadError("temporary lens connection interrupted")
        return original(target)
    monkeypatch.setattr(harness.controller, "move_to", transient)
    harness.warm()
    if factor == 3:
        harness.start_cycle()
        assert len(harness.manager.catalog) == 1
    assert attempts.count(factor) == 2
    assert harness.manager.status()["error"] is None
    assert harness.manager.status()["zoom_recovery"]["failure_kind"] is None


def test_deadline_during_network_retry_retains_transport_recovery_and_ignores_late_ack(cycle_factory, monkeypatch):
    import httpx
    harness = cycle_factory()
    harness.warm()
    original = harness.controller.move_to
    attempts = []
    def interrupted_then_slow(target):
        attempts.append(target)
        if target == 3:
            count = attempts.count(3)
            if count == 1:
                raise httpx.ConnectTimeout("temporary control interruption")
            if count == 2:
                harness.controller.entered.set()
                assert harness.controller.release.wait(5)
        return original(target)
    monkeypatch.setattr(harness.controller, "move_to", interrupted_then_slow)
    clock = [1000.]
    monkeypatch.setattr(optical_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    harness.feed([ship()])
    assert harness.controller.entered.wait(3)
    recording_id = harness.manager.status()["active_id"]
    clock[0] += 4
    status = harness.feed()
    assert status["phase"] == "recovering"
    assert status["zoom_recovery"]["failure_kind"] == "transport"
    assert status["active_id"] == recording_id
    assert status["current_zoom_factor"] is None
    harness.controller.release.set()
    harness.drain_control()
    # The delayed 3x acknowledgement belongs to an invalidated generation.
    assert harness.manager.status()["phase"] == "recovering"
    assert harness.manager.status()["current_zoom_factor"] is None
    clock[0] += 5
    harness.feed()
    harness.phase("zoomed")
    assert harness.manager.status()["active_id"] == recording_id
    assert harness.manager.status()["current_zoom_factor"] == 1
    assert harness.manager.status()["zoom_recovery"]["fallback_recording"] is True


def test_transient_lens_retries_are_bounded(cycle_factory, monkeypatch):
    import httpx
    harness = cycle_factory()
    attempts = []
    def unavailable(target):
        attempts.append(target)
        raise httpx.ConnectError("lens connection unavailable")
    monkeypatch.setattr(harness.controller, "move_to", unavailable)
    clock = [1000.]
    monkeypatch.setattr(optical_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    harness.feed()
    harness.phase("recovering")
    assert attempts == [1., 1.]
    for _ in range(4):
        clock[0] += 31
        harness.feed()
        harness.drain_control()
    harness.phase("error")
    assert attempts == [1.] * 6
    assert harness.manager.status()["active_id"] is None
    # Exhaustion stops movement attempts, but later read-only confirmation
    # can recover without another manual restart or inventing a position.
    assert harness.manager.optical_control_busy() is False
    generation = harness.manager.zoom_observation_context()["generation"]
    harness.manager.observe_zoom_factor(3, generation)
    assert harness.manager.status()["phase"] == "error"
    harness.manager.observe_zoom_factor(1, generation)
    harness.phase("overview")
    assert harness.manager.status()["error"] is None
    assert attempts == [1.] * 6


def test_transport_failure_keeps_video_until_confirmed_panorama_and_target_departure(cycle_factory, monkeypatch):
    import httpx
    harness = cycle_factory()
    harness.warm()
    original = harness.controller.move_to
    attempts = []
    def fail_first_zoom(target):
        attempts.append(target)
        if target == 3 and attempts.count(3) <= 2:
            raise httpx.ReadTimeout("temporary control timeout")
        return original(target)
    monkeypatch.setattr(harness.controller, "move_to", fail_first_zoom)
    clock = [1000.]
    monkeypatch.setattr(optical_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    harness.feed([ship()])
    harness.phase("recovering")
    recording_id = harness.manager.status()["active_id"]
    assert recording_id
    before = len([event for event in harness.events if event[0] == "frame"])
    for _ in range(5): harness.feed([ship()])
    assert len([event for event in harness.events if event[0] == "frame"]) > before
    assert harness.manager.status()["current_zoom_factor"] is None
    assert harness.manager.catalog[recording_id]["status"] == "recording"
    clock[0] += 5
    harness.feed()
    harness.phase("zoomed")
    assert harness.manager.status()["current_zoom_factor"] == 1
    assert harness.manager.status()["error"] is None
    assert harness.manager.status()["active_id"] == recording_id
    assert harness.manager.status()["zoom_recovery"]["fallback_recording"] is True
    harness.feed([ship()])
    harness.depart()
    harness.phase("overview")
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    item = harness.manager.catalog[recording_id]
    assert item["returned_to_overview"] is True
    assert item["zoom_fallback"] is True
    assert item["failed_zoom_factor"] == 3
    assert item["status"] == "interrupted"
    assert harness.manager.status()["zoom_recovery"]["failure_kind"] is None
    harness.start_cycle()
    assert len(harness.manager.catalog) == 2


def test_unknown_lens_fallback_is_bounded_even_with_continuous_background_detections(cycle_factory, monkeypatch):
    import httpx
    harness = cycle_factory(max_clip_seconds=5)
    harness.warm()
    monkeypatch.setattr(harness.controller, "move_to", lambda _factor: (_ for _ in ()).throw(httpx.ReadTimeout("timeout")))
    clock = [1000.]
    monkeypatch.setattr(optical_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    harness.feed([ship()])
    harness.phase("recovering")
    recording_id = harness.manager.status()["active_id"]
    for _ in range(4):
        clock[0] += 31
        harness.feed([ship()])
        harness.drain_control()
    harness.phase("error")
    assert harness.manager.status()["active_id"] == recording_id
    assert harness.manager.status()["current_zoom_factor"] is None
    harness.feed([ship()], advance=6)
    harness.drain_control()
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    assert harness.manager.status()["active_id"] is None
    assert harness.manager.catalog[recording_id]["status"] == "interrupted"
    assert harness.manager.catalog[recording_id]["returned_to_overview"] is False


def test_explicit_source_stop_cancels_pending_fallback_recovery(cycle_factory, monkeypatch):
    import httpx
    harness = cycle_factory()
    harness.warm()
    original = harness.controller.move_to
    attempts = []
    def unavailable_zoom(target):
        attempts.append(target)
        if target != 1: raise httpx.ReadTimeout("timeout")
        return original(target)
    monkeypatch.setattr(harness.controller, "move_to", unavailable_zoom)
    clock = [1000.]
    monkeypatch.setattr(optical_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    harness.feed([ship()])
    harness.phase("recovering")
    harness.manager.finish("source_stopped")
    harness.drain_control()
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    completed = list(attempts)
    clock[0] += 60
    harness.feed()
    assert attempts == completed
    assert harness.manager.status()["active_id"] is None


@pytest.mark.parametrize("observations", ["background", "no_inference", "stale", "frame_local_id"])
def test_unknown_view_closes_media_after_target_evidence_ends_without_waiting_for_motor(cycle_factory, monkeypatch, observations):
    import httpx
    harness = cycle_factory()
    harness.warm()
    original = harness.controller.move_to
    network_restored = [False]
    def unavailable(target):
        if not network_restored[0]:
            raise httpx.ConnectTimeout("lens unavailable")
        return original(target)
    monkeypatch.setattr(harness.controller, "move_to", unavailable)
    clock = [1000.]
    monkeypatch.setattr(optical_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    harness.feed([ship()])
    harness.phase("recovering")
    recording_id = harness.manager.status()["active_id"]
    for _ in range(40):
        status = harness.feed([ship()], advance=.25)
        assert status["active_id"] == recording_id
    last_target = harness.timestamp
    stale_context = {**harness.manager.zoom_observation_context(), "generation": -1}
    background = {**ship(), "ship_id": 99}
    reused_frame_id = {**ship(), "confidence": .32,
                       "boxes": [{"type": "ship", "xywh": [1, 1, 2, 2]}]}
    for _ in range(24):
        if observations == "background":
            harness.feed([background, reused_frame_id], advance=.25)
        elif observations == "frame_local_id":
            harness.feed([{**ship(), "tracked": False}], advance=.25)
        elif observations == "stale":
            harness.feed([ship()], context=stale_context, advance=.25)
        else:
            harness.feed(advance=.25)
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    item = harness.manager.catalog[recording_id]
    assert harness.manager.status()["active_id"] is None
    assert item["status"] == "interrupted"
    assert item["media_end_reason"] == "target_evidence_timeout"
    assert item["source_end_seconds"] <= last_target + .3 + .001
    assert item["duration_seconds"] <= last_target - item["source_start_seconds"] + .5
    assert item["returned_to_overview"] is False
    # Recovery proceeds without retaining the now-closed writer. It cannot
    # start a fresh cycle until the camera actually confirms panorama.
    harness.feed([background], advance=3)
    assert harness.manager.status()["active_id"] is None
    network_restored[0] = True
    clock[0] += 6
    harness.feed()
    harness.phase("overview")
    assert item["returned_to_overview"] is True
    assert harness.manager.cycle_count == 1
    assert item["source_end_seconds"] <= last_target + .301
    harness.start_cycle()
    assert len(harness.manager.catalog) == 2


def test_cycle_opens_recording_before_requested_optical_zoom(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    recording_id = harness.start_cycle()
    events = harness.events
    assert events.index(("recording_open", recording_id)) < events.index(("move_requested", 3))
    assert events.index(("move_requested", 3)) < events.index(("move_settled", 3))
    status = harness.manager.status()
    assert status["current_zoom_factor"] == 3
    assert status["zoom_driver"] == "optical"
    assert status["phase"] == "zoomed"


def test_tail_departure_freezes_video_before_waiting_for_verified_panorama(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    recording_id = harness.start_cycle()
    harness.controller.block_factor = 1
    harness.depart()
    assert harness.controller.entered.wait(3)
    assert harness.manager.status()["active_id"] == recording_id
    assert harness.manager.catalog[recording_id]["status"] == "recording"
    assert ("recording_close", recording_id) in harness.events
    assert harness.manager.status()["media_recording"] is False
    duration = harness.manager.catalog[recording_id]["duration_seconds"]
    # Motor return is still this cycle, but its idle frames no longer extend
    # the video after confirmed target departure.
    harness.feed([ship()])
    harness.feed([ship()], advance=10)
    assert harness.manager.catalog[recording_id]["duration_seconds"] == duration
    assert len(harness.manager.list_recordings()["items"]) == 1
    harness.controller.release.set()
    harness.phase("overview")
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    assert harness.manager.catalog[recording_id]["status"] == "completed"
    assert harness.events.index(("recording_close", recording_id)) < harness.events.index(("move_settled", 1), harness.events.index(("move_settled", 3)))
    assert harness.manager.status()["current_zoom_factor"] == 1


def test_departed_video_is_finalized_even_if_panorama_return_cannot_be_confirmed(cycle_factory, monkeypatch):
    import httpx
    harness = cycle_factory()
    harness.warm()
    recording_id = harness.start_cycle()
    original = harness.controller.move_to
    restored = [False]
    def disconnected_return(target):
        if target == 1 and not restored[0]:
            raise httpx.ConnectTimeout("return unavailable")
        return original(target)
    monkeypatch.setattr(harness.controller, "move_to", disconnected_return)
    clock = [1000.]
    monkeypatch.setattr(optical_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    harness.depart()
    harness.phase("recovering")
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    item = harness.manager.catalog[recording_id]
    assert harness.manager.status()["active_id"] is None
    assert item["status"] == "interrupted"
    duration = item["duration_seconds"]
    harness.feed([], advance=60)
    assert item["duration_seconds"] == duration
    restored[0] = True
    clock[0] += 5
    harness.feed()
    harness.phase("overview")
    assert item["returned_to_overview"] is True
    assert item["status"] == "interrupted"


def test_fresh_panorama_repeats_even_when_same_ship_track_is_still_visible(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    first = harness.start_cycle()
    stale_context = dict(harness.manager.zoom_observation_context())
    harness.depart()
    harness.phase("overview")
    harness.feed([ship()], context=stale_context)
    assert harness.manager.status()["active_id"] is None
    assert len(harness.manager.list_recordings()["items"]) == 1
    second = harness.start_cycle()
    assert second != first
    assert len(harness.manager.list_recordings()["items"]) == 2
    assert harness.controller.calls[:4] == [1, 3, 1, 3]


def test_transient_empty_scene_and_no_new_inference_do_not_end_a_pass(cycle_factory):
    harness = cycle_factory(departure_confirm_seconds=.5)
    harness.warm()
    recording_id = harness.start_cycle()
    harness.feed([])
    harness.feed(None, advance=.2)
    harness.feed([ship()])
    assert harness.manager.status()["active_id"] == recording_id
    assert harness.manager.status()["phase"] == "zoomed"
    assert harness.controller.calls == [1, 3]


def test_old_panorama_results_cannot_end_a_zoomed_pass(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    old_context = dict(harness.manager.zoom_observation_context())
    recording_id = harness.start_cycle()
    for _ in range(7):
        harness.feed([], context=old_context)
    assert harness.manager.status()["phase"] == "zoomed"
    assert harness.manager.status()["active_id"] == recording_id
    assert harness.controller.calls == [1, 3]


def test_empty_zoomed_view_waits_for_the_bow_before_detecting_stern_departure(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    harness.feed([ship()])
    recording_id = harness.manager.status()["active_id"]
    harness.phase("zoomed")
    # The bow detected at the edge of the 1x panorama may not yet have reached
    # the smaller optical field of view. Empty frames here are not an exit.
    for _ in range(8):
        status = harness.feed([])
    assert status["active_id"] == recording_id
    assert status["phase"] == "zoomed"
    assert status["awaiting_zoomed_target"] is True
    assert harness.controller.calls == [1, 3]
    status = harness.feed([ship()])
    assert status["awaiting_zoomed_target"] is False
    harness.depart()
    harness.phase("overview")
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    assert harness.manager.catalog[recording_id]["status"] == "completed"
    assert harness.controller.calls == [1, 3, 1]


@pytest.mark.parametrize("operation", ["finish", "disable"])
def test_source_stop_or_disable_requests_panorama_without_new_frames(cycle_factory, operation):
    harness = cycle_factory()
    harness.warm()
    recording_id = harness.start_cycle()
    if operation == "finish":
        harness.manager.finish("source_stopped")
    else:
        harness.manager.update_settings({"enabled": False})
    harness.drain_control()
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    assert harness.controller.calls[-1] == 1
    assert ("recording_close", recording_id) in harness.events
    assert harness.manager.status()["active_id"] is None
    assert harness.manager.catalog[recording_id]["status"] in {"interrupted", "completed"}


def test_failed_zoom_never_claims_target_magnification_or_starts_another_clip(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    harness.controller.fail_factor = 3
    harness.feed([ship()])
    for _ in range(5):
        harness.drain_control()
        status = harness.feed()
        if status["state"] == "error":
            break
    assert status["state"] == "error"
    assert status["error"]
    assert status["current_zoom_factor"] != 3
    existing = len(harness.manager.list_recordings()["items"])
    harness.feed([ship()])
    harness.drain_control()
    assert len(harness.manager.list_recordings()["items"]) == existing


def test_saving_settings_after_motor_error_can_restart_with_fresh_panorama(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    harness.controller.fail_factor = 3
    harness.feed([ship()])
    harness.phase("error")
    assert harness.manager.status()["error"]
    harness.controller.fail_factor = None
    harness.manager.update_settings({"enabled": True})
    harness.warm()
    assert harness.manager.status()["error"] is None
    assert harness.manager.status()["current_zoom_factor"] == 1
    harness.start_cycle()
    assert harness.manager.status()["current_zoom_factor"] == 3
    assert len(harness.manager.list_recordings()["items"]) == 2


def test_old_camera_return_does_not_close_new_local_recording(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    camera_recording = harness.start_cycle()
    harness.controller.block_factor = 1
    harness.manager.finish("source_stopped")
    assert harness.controller.entered.wait(3)
    harness.manager.process_frame(
        np.full((48, 64, 3), 100, dtype=np.uint8),
        camera_id="local-video", session_id="local-session", source_name="local.mp4",
        source_type="local_video", fps=10, timestamp=0, vessels=[ship()],
    )
    local_recording = harness.manager.status()["active_id"]
    assert local_recording and local_recording != camera_recording
    harness.controller.release.set()
    harness.drain_control()
    harness.drain_control()
    assert harness.manager.status()["active_id"] == local_recording
    assert harness.manager.catalog[local_recording]["status"] == "recording"
    assert ("recording_close", local_recording) not in harness.events


def test_shutdown_drain_waits_for_the_return_queued_after_zoom_in(cycle_factory):
    harness = cycle_factory()
    harness.warm()
    harness.controller.block_factor = 3
    harness.feed([ship()])
    assert harness.controller.entered.wait(3)
    recording_id = harness.manager.status()["active_id"]
    harness.manager.finish("service_shutdown")
    harness.controller.release.set()
    assert harness.manager.wait_for_optical_stop(timeout=3) is True
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    assert harness.controller.calls == [1, 3, 1]
    assert harness.manager.status()["current_zoom_factor"] == 1
    assert harness.manager.status()["active_id"] is None
    assert ("recording_close", recording_id) in harness.events


def test_unsettled_lens_times_out_instead_of_claiming_success(cycle_factory, monkeypatch):
    harness = cycle_factory()
    harness.warm()
    clock = [1000.]
    # Replace only this module's time reference, not the process-wide clock
    # used by Future/thread timeout implementation.
    monkeypatch.setattr(optical_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    harness.controller.block_factor = 3
    harness.feed([ship()])
    assert harness.controller.entered.wait(3)
    clock[0] += 4
    status = harness.feed()
    assert status["state"] == "error"
    assert status["error"]
    assert status["current_zoom_factor"] != 3
    harness.controller.release.set()
    harness.drain_control()
    harness.feed([ship()])
    harness.drain_control()
    assert len(harness.manager.list_recordings()["items"]) == 1
    assert harness.manager.status()["current_zoom_factor"] != 3


def test_optical_cycle_produces_one_playable_file_with_full_camera_frames(cycle_factory):
    harness = cycle_factory(real_video=True)
    harness.warm()
    recording_id = harness.start_cycle()
    for _ in range(5):
        harness.feed([ship()])
    harness.depart()
    harness.phase("overview")
    harness.manager.executor.submit(lambda: None).result(timeout=30)
    item = next(item for item in harness.manager.list_recordings()["items"] if item["id"] == recording_id)
    assert item["status"] == "completed"
    assert item["has_focus"] is False
    assert item["focused_url"] is None
    assert item["cycle_url"]
    media = harness.manager.media_path(recording_id, "cycle")
    assert len(list(media.parent.glob("*.mp4"))) == 1
    capture = cv2.VideoCapture(str(media))
    try:
        assert capture.isOpened()
        assert capture.get(cv2.CAP_PROP_FRAME_COUNT) > 5
        assert capture.get(cv2.CAP_PROP_FRAME_WIDTH) == 64
        assert capture.get(cv2.CAP_PROP_FRAME_HEIGHT) == 48
    finally:
        capture.release()
