from dataclasses import replace
from types import SimpleNamespace
import threading
import time
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from app import camera_motion, detail_focus
from app.live_capture import OnvifControl
from app.config import Settings
from app.inference.scheduler import InferenceScheduler


def test_regional_sharpness_excludes_background_and_rejects_tiny_area():
    image = np.zeros((120, 160, 3), np.uint8)
    image[:, 80:] = np.random.default_rng(42).integers(0, 256, (120, 80, 3), dtype=np.uint8)
    assert detail_focus.sharpness(image, (0, 0, .45, 1)) == 0
    assert detail_focus.sharpness(image, (.55, 0, 1, 1)) > 100
    with pytest.raises(ValueError): detail_focus.sharpness(image, (0, 0, .01, .01))


def test_bounded_focus_search_returns_best_measured_position(monkeypatch):
    position, moves = [.5], []
    def move(target): position[0] = target; moves.append(target)
    control = SimpleNamespace(focus_options=lambda: {"min": 0., "max": 1.}, focus_position=lambda: position[0], move_focus=move)
    monkeypatch.setattr(detail_focus, "sharpness", lambda value, region: value)
    result = detail_focus.search_focus(control, lambda after: 1000 - ((position[0] - .52) * 1000) ** 2, lambda: False, (0, 0, 1, 1))
    assert result["focus_position"] == pytest.approx(.52)
    assert result["sharpness_best"] > result["sharpness_before"]
    assert len(moves) <= 5
    assert all(.475 <= target <= .545 for target in moves)
    assert moves[-1] == pytest.approx(.52)


def test_focus_failure_restores_original_position(monkeypatch):
    moves = []
    control = SimpleNamespace(focus_options=lambda: {"min": 0., "max": 1.}, focus_position=lambda: .5, move_focus=moves.append)
    samples = iter([100, 100])
    def sample(after):
        try: return next(samples)
        except StopIteration: raise TimeoutError("no new frames")
    monkeypatch.setattr(detail_focus, "sharpness", lambda value, region: value)
    with pytest.raises(TimeoutError): detail_focus.search_focus(control, sample, lambda: False, (0, 0, 1, 1))
    assert moves[-1] == .5


def test_textureless_region_does_not_move_lens():
    moves = []
    control = SimpleNamespace(focus_options=lambda: {"min": 0., "max": 1.}, focus_position=lambda: .5, move_focus=moves.append)
    with pytest.raises(ValueError, match="纹理不足"):
        detail_focus.search_focus(control, lambda after: np.zeros((32, 32, 3), np.uint8), lambda: False, (0, 0, 1, 1))
    assert moves == []


def test_focus_cancel_does_not_start_another_move(monkeypatch):
    moves = []
    control = SimpleNamespace(focus_options=lambda: {"min": 0., "max": 1.}, focus_position=lambda: .5, move_focus=moves.append)
    monkeypatch.setattr(detail_focus, "sharpness", lambda value, region: 100)
    with pytest.raises(InterruptedError):
        detail_focus.search_focus(control, lambda after: 100, lambda: True, (0, 0, 1, 1))
    assert moves == []


def test_focus_rejects_stale_capture_and_cancels_after_generation_change():
    worker = detail_focus.DetailFocus()
    try:
        with pytest.raises(ValueError, match="启动摄像头"):
            worker.start(None, (0, 0, 1, 1))
        worker.observe(np.random.default_rng(1).integers(0, 256, (32, 32, 3), dtype=np.uint8), {"generation": 1})
        started = threading.Event()
        def options(): started.set(); return {"min": 0., "max": 1.}
        control = SimpleNamespace(lock=threading.RLock(), focus_options=options, focus_position=lambda: .5)
        worker.start(control, (0, 0, 1, 1))
        assert started.wait(1)
        worker.observe(np.zeros((32, 32, 3), np.uint8), {"generation": 2})
        worker.future.result(timeout=3)
        assert worker.snapshot()["status"] == "cancelled"
    finally:
        worker.stop()
        worker.executor.shutdown(wait=True)


def test_auto_region_focus_uses_name_roi_without_requiring_recognition_models(monkeypatch):
    worker = detail_focus.DetailFocus()
    try:
        manager = SimpleNamespace(get_settings=lambda: {"auto_region_focus": True, "enable_ship_name": False, "enable_draft": False},
            status=lambda: {"phase": "zoomed", "active_id": "clip", "locked_target": {"ship_id": 2}},
            zoom_observation_context=lambda: {"generation": 5}, record_focus_event=lambda *_: None)
        live = {"source_width": 1000, "source_height": 500, "vessels": [{"ship_id": 2, "tracked": True,
            "boxes": [{"type": "waterline_area", "xywh": [100, 200, 200, 50]}, {"type": "ship_name_area", "xywh": [200, 100, 100, 50]}]}]}
        calls = []
        monkeypatch.setattr(worker, "start", lambda control, roi, **kwargs: calls.append(roi))
        worker.consider(None, live, {"generation": 4, "accept_detections": True}, manager)
        assert not calls
        worker.consider(None, live, {"generation": 5, "accept_detections": True}, manager)
        assert calls == [(.2, .2, .3, .3)]
        worker.consider(None, live, {"generation": 5, "accept_detections": True}, manager)
        assert len(calls) == 1
    finally:
        worker.executor.shutdown(wait=True)


def test_focus_metadata_write_failure_does_not_block_capture_teardown(monkeypatch):
    worker = detail_focus.DetailFocus()
    try:
        worker.observe(np.ones((32, 32, 3), np.uint8), {"generation": 1})
        control = SimpleNamespace(lock=threading.RLock())
        monkeypatch.setattr(detail_focus, "search_focus", lambda *args: {"focus_position": .5})
        def fail(_state): raise OSError("storage unavailable")
        worker.start(control, (0, 0, 1, 1), completed=fail)
        assert worker.wait()
        assert worker.snapshot()["status"] == "completed"
        assert "保存失败" in worker.snapshot()["error"]
    finally:
        worker.executor.shutdown(wait=True)


def motion_xml(body):
    return ET.fromstring(f'<root xmlns:tt="{OnvifControl.SCHEMA}">{body}</root>')


def test_pan_direction_uses_declared_space_and_always_stops(monkeypatch):
    control = OnvifControl()
    calls = []
    monkeypatch.setattr(control, "motion_options", lambda: {"continuous": {"space": "camera-space", "x": {"min": -2, "max": 2}, "y": {"min": -1, "max": 1}}, "timeout": "PT1S"})
    monkeypatch.setattr(camera_motion.time, "sleep", lambda _: None)
    def request(settings, ns, action, body):
        calls.append((action, body))
        if action == "ContinuousMove": raise RuntimeError("connection lost after dispatch")
    monkeypatch.setattr(control, "_request", request)
    with pytest.raises(RuntimeError): control.pan_tilt("pan_left")
    assert calls[-1][0] == "Stop"
    assert 'x="-0.3"' in calls[0][1] and "camera-space" in calls[0][1]


def test_focus_position_requires_actual_idle_feedback(monkeypatch):
    control = OnvifControl()
    monkeypatch.setattr(control, "_request", lambda *_: motion_xml('<tt:FocusStatus20><tt:Position>0.52</tt:Position><tt:MoveStatus>IDLE</tt:MoveStatus></tt:FocusStatus20>'))
    assert control.focus_position() == .52
    monkeypatch.setattr(control, "_request", lambda *_: motion_xml('<tt:FocusStatus20><tt:Position>0.52</tt:Position><tt:MoveStatus>MOVING</tt:MoveStatus></tt:FocusStatus20>'))
    with pytest.raises(ValueError): control.focus_position()


def test_monitor_view_is_saved_locally_and_restore_requires_position_feedback(tmp_path, monkeypatch):
    settings = replace(Settings(), camera_onvif_ptz_url="http://camera/ptz", camera_onvif_source_token="source")
    monkeypatch.setattr(camera_motion, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(camera_motion, "PLATFORM_ROOT", tmp_path)
    control = OnvifControl()
    monkeypatch.setattr(control, "motion_options", lambda: {"absolute": {"space": "native-pan", "x": {"min": -1, "max": 1}, "y": {"min": -1, "max": 1}}})
    monkeypatch.setattr(control, "read_magnification", lambda: 1)
    position = {"x": .2, "y": -.1, "space": "native-pan"}
    monkeypatch.setattr(control, "_pan_position", lambda settings: dict(position))
    calls = []
    monkeypatch.setattr(control, "_request", lambda _s, _ns, action, body: calls.append((action, body)))
    monkeypatch.setattr(control, "move_to", lambda factor: calls.append(("zoom", factor)))
    control.monitor_view()
    assert calls == []
    assert (tmp_path / "data" / "monitor_view.json").is_file()
    control.monitor_view(restore=True)
    assert calls[0] == ("zoom", 1)
    assert calls[1][0] == "AbsoluteMove" and 'x="0.2"' in calls[1][1]
    assert calls[-1][0] == "Stop"
    monkeypatch.setattr(control, "_pan_position", lambda settings: {**position, "space": "wrong-space"})
    with pytest.raises(ValueError, match="坐标空间"):
        control.monitor_view(restore=True)
    assert calls[-1][0] == "Stop"


def test_monitor_view_refuses_to_save_unknown_zoom(tmp_path, monkeypatch):
    monkeypatch.setattr(camera_motion, "PLATFORM_ROOT", tmp_path)
    control = OnvifControl()
    monkeypatch.setattr(control, "motion_options", lambda: {"absolute": {"space": "native"}})
    monkeypatch.setattr(control, "read_magnification", lambda: None)
    with pytest.raises(ValueError, match="1×"):
        control.monitor_view()
    assert not (tmp_path / "data" / "monitor_view.json").exists()


def test_live_waiter_has_priority_over_another_batch_frame():
    scheduler = InferenceScheduler()
    order = []
    def run(realtime):
        with scheduler.slot(realtime=realtime): order.append("live" if realtime else "batch")
    with scheduler.slot():
        batch = threading.Thread(target=run, args=(False,))
        live = threading.Thread(target=run, args=(True,))
        batch.start(); live.start()
        deadline = time.monotonic() + 2
        while not scheduler.live_waiters and time.monotonic() < deadline: time.sleep(.005)
        assert scheduler.live_waiters == 1
    live.join(2); batch.join(2)
    assert order == ["live", "batch"]


def test_cancel_waiting_batch_does_not_release_other_workers_slot():
    scheduler = InferenceScheduler()
    cancelled = []
    def wait():
        try:
            with scheduler.slot(cancelled=lambda: True): pytest.fail("cancelled job acquired the GPU")
        except InterruptedError: cancelled.append(True)
    with scheduler.slot():
        thread = threading.Thread(target=wait)
        thread.start(); thread.join(2)
        assert scheduler.busy
    assert cancelled == [True] and not scheduler.busy
