from dataclasses import replace
import threading
import xml.etree.ElementTree as ET

import httpx
import pytest

from app import horizontal_follow
from app.config import Settings
from app.live_capture import OnvifControl


@pytest.fixture
def device(tmp_path, monkeypatch):
    settings = replace(Settings(), camera_onvif_ptz_url="http://camera/ptz", camera_onvif_source_token="sensor",
        camera_onvif_ptz_configuration_token="config", camera_onvif_zoom_timeout=2)
    monkeypatch.setattr(horizontal_follow, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(horizontal_follow, "PLATFORM_ROOT", tmp_path)
    control = OnvifControl()
    options = {"absolute": {"space": "absolute", "x": {"min": -1, "max": 1}, "y": {"min": -1, "max": 1}},
               "fov": {"space": "camera/TranslationSpaceFov", "x": {"min": -1, "max": 1}, "y": {"min": -1, "max": 1}}}
    state = {"x": .2, "y": -.15, "space": "absolute"}
    calls = []
    control.motion_options = lambda: options
    control._pan_position = lambda settings: dict(state)
    control.read_magnification = lambda: 1.
    def request(settings, ns, action, body):
        xml = ET.fromstring(f'<root xmlns:t="{control.PTZ}" xmlns:tt="{control.SCHEMA}">{body}</root>')
        pan = xml.find(f'.//{{{control.SCHEMA}}}PanTilt')
        calls.append((action, dict(pan.attrib) if pan is not None else {}))
        if action == "RelativeMove":
            state["x"] += float(pan.attrib["x"]) * .1
            state["y"] += float(pan.attrib["y"]) * .1
        elif action == "AbsoluteMove":
            state.update(x=float(pan.attrib["x"]), y=float(pan.attrib["y"]))
        return ET.Element("response")
    control._request = request
    return control, state, calls, settings, tmp_path


@pytest.mark.parametrize("step", [-.3, .3])
def test_actual_horizontal_feedback_and_return_do_not_change_tilt(device, step):
    control, state, calls, _, root = device
    original = dict(state)
    control.begin_horizontal_follow()
    assert not calls
    result = control.follow_horizontal(step, max_travel_ratio=.15, zoom_factor=1)
    assert result["settled"] and result["fov_x"] == step
    relative = next(attrs for action, attrs in calls if action == "RelativeMove")
    assert relative["y"] == "0" and relative["space"].endswith("TranslationSpaceFov")
    assert state["y"] == original["y"]
    assert calls[-1][0] == "Stop"
    control.restore_horizontal_follow()
    assert state == original
    absolute = next(attrs for action, attrs in calls if action == "AbsoluteMove")
    assert float(absolute["y"]) == original["y"]
    assert (root / "data" / "horizontal_follow_origin.json").is_file()


def test_pending_origin_is_recovered_after_a_process_restart(device):
    control, state, calls, _, _ = device
    original = state["x"]
    control.begin_horizontal_follow()
    control.follow_horizontal(.3, max_travel_ratio=.15, zoom_factor=1)
    other = OnvifControl()
    other.motion_options, other._pan_position, other._request = control.motion_options, control._pan_position, control._request
    other.begin_horizontal_follow()
    assert state["x"] == pytest.approx(original)
    assert other._follow_origin["displaced"] is False
    assert sum(action == "AbsoluteMove" for action, _ in calls) == 1


def test_travel_limit_stops_further_panning_but_allows_home_return(device):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    result = control.follow_horizontal(.3, max_travel_ratio=.01, zoom_factor=1)
    assert result["limited"]
    count = len(calls)
    result = control.follow_horizontal(.3, max_travel_ratio=.01, zoom_factor=1)
    assert result["limited"] and result["fov_x"] == 0
    assert len(calls) == count
    control.restore_horizontal_follow()
    assert state["x"] == .2


def test_changed_zoom_does_not_dispatch_a_pan_command(device):
    control, _, calls, _, _ = device
    control.begin_horizontal_follow()
    control.read_magnification = lambda: 3
    with pytest.raises(RuntimeError, match="倍率"):
        control.follow_horizontal(.2, max_travel_ratio=.15, zoom_factor=1)
    assert calls == []


def test_external_pan_is_not_treated_as_our_predicted_view_offset(device):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    state["x"] += .2
    with pytest.raises(RuntimeError, match="其他控制端"):
        control.follow_horizontal(.2, max_travel_ratio=.15, zoom_factor=1)
    assert not calls


def test_small_acknowledged_vertical_feedback_drift_does_not_accumulate_as_external_tilt(device):
    control, state, calls, _, _ = device
    original = control._request
    def request(*args):
        result = original(*args)
        if args[2] == "RelativeMove": state["y"] += .001
        return result
    control._request = request
    control.begin_horizontal_follow()
    for _ in range(8):
        assert control.follow_horizontal(.3, max_travel_ratio=.4, zoom_factor=1)["settled"]
    assert sum(action == "RelativeMove" for action, _ in calls) == 8
    assert all(attrs.get("y") == "0" for action, attrs in calls if action == "RelativeMove")


def test_intervening_external_vertical_jump_still_prevents_horizontal_dispatch(device):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    state["y"] += .03
    with pytest.raises(RuntimeError, match="外部上下移动"):
        control.follow_horizontal(.2, max_travel_ratio=.15, zoom_factor=1)
    assert not calls


def test_uncertain_move_still_stops_and_restores_the_original_position(device):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    original = control._request
    def uncertain(settings, ns, action, body):
        result = original(settings, ns, action, body)
        if action == "RelativeMove": raise httpx.ReadTimeout("reply lost after dispatch")
        return result
    control._request = uncertain
    with pytest.raises(httpx.ReadTimeout):
        control.follow_horizontal(.2, max_travel_ratio=.15, zoom_factor=1)
    assert calls[-1][0] == "Stop" and control._follow_origin["displaced"]
    control.restore_horizontal_follow()
    assert state["x"] == .2


@pytest.mark.parametrize("step", [float("nan"), float("inf"), 0, .7, -.7])
def test_invalid_follow_step_never_moves_the_camera(device, step):
    control, _, calls, _, _ = device
    with pytest.raises(ValueError): control.follow_horizontal(step, max_travel_ratio=.15, zoom_factor=1)
    assert not calls


def test_fov_support_is_required_instead_of_guessing_a_native_delta(device):
    control, _, calls, _, _ = device
    control.motion_options = lambda: {"absolute": {}}
    with pytest.raises(ValueError, match="FOV"):
        control.begin_horizontal_follow()
    assert not calls


def test_return_preserves_an_externally_changed_vertical_position(device):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    control.follow_horizontal(.2, max_travel_ratio=.15, zoom_factor=1)
    state["y"] = .4
    control.restore_horizontal_follow()
    assert state["y"] == .4
    assert float(next(attrs for action, attrs in calls if action == "AbsoluteMove")["y"]) == .4


def test_stop_status_lag_is_polled_before_returning_home(device, monkeypatch):
    control, state, calls, _, _ = device
    moving_reads = [0]
    old_position, old_request = control._pan_position, control._request
    def position(settings):
        if moving_reads[0]:
            moving_reads[0] -= 1
            raise ValueError("无法确认云台停止及实际位置")
        return old_position(settings)
    def request(*args):
        result = old_request(*args)
        if args[2] == "Stop": moving_reads[0] = 2
        return result
    control._pan_position, control._request = position, request
    monkeypatch.setattr(horizontal_follow.time, "sleep", lambda _: None)
    control.begin_horizontal_follow()
    control.follow_horizontal(.2, max_travel_ratio=.15, zoom_factor=1)
    control.restore_horizontal_follow()
    assert state["x"] == .2
    assert sum(action == "RelativeMove" for action, _ in calls) == 1


def test_horizontal_only_return_checks_the_origin_even_after_external_movement(device):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    state["x"] = .4
    control.restore_horizontal_follow()
    assert state["x"] == .2
    assert calls[-1][0] == "Stop"


def test_pan_cancellation_prevents_dispatch(device):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    control.cancel_move()
    with pytest.raises(RuntimeError, match="取消"):
        control.follow_horizontal(.2, max_travel_ratio=.15, zoom_factor=1)
    assert not calls


def test_brief_moving_zoom_feedback_is_waited_out_before_dispatch(device, monkeypatch):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    replies = iter([None, None, 1., None, 1.])
    control.read_magnification = lambda: next(replies)
    monkeypatch.setattr(horizontal_follow.time, "sleep", lambda _: None)
    assert control.follow_horizontal(.2, max_travel_ratio=.15, zoom_factor=1)["settled"]
    assert sum(action == "RelativeMove" for action, _ in calls) == 1


def test_observed_native_gain_limits_the_last_step_before_range_boundary(device):
    control, state, calls, _, _ = device
    control.begin_horizontal_follow()
    for _ in range(15):
        result = control.follow_horizontal(.3, max_travel_ratio=.15, zoom_factor=1)
        if result["limited"]: break
    assert result["limited"]
    assert 0 < state["x"] - .2 <= .3
    assert abs(float([attrs for action, attrs in calls if action == "RelativeMove"][-1]["x"])) < .3


def test_uncalibrated_first_pan_cannot_wrap_the_native_boundary(device):
    control, state, calls, _, _ = device
    state["x"] = .99
    control.begin_horizontal_follow()
    result = control.follow_horizontal(.3, max_travel_ratio=.15, zoom_factor=1)
    assert result["limited"] and result["fov_x"] == 0
    assert not calls and state["x"] == .99
