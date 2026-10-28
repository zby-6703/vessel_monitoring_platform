from dataclasses import replace
import xml.etree.ElementTree as ET

import pytest

from app import live_capture
from app.config import get_settings


SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/ZoomGenericSpace"
SCHEMA = "http://www.onvif.org/ver10/schema"


def xml(body):
    return ET.fromstring(f'<root xmlns:tt="{SCHEMA}" xmlns:t="{live_capture.OnvifControl.PTZ}">{body}</root>')


def zoom_status(position=.1, movement="IDLE", space=SPACE):
    move = f"<tt:MoveStatus><tt:Zoom>{movement}</tt:Zoom></tt:MoveStatus>" if movement else ""
    return xml(f'<t:PTZStatus><tt:Position><tt:Zoom x="{position}" space="{space}"/></tt:Position>{move}</t:PTZStatus>')


def options(space=SPACE, minimum=0, maximum=1):
    return xml(f"<tt:Spaces><tt:AbsoluteZoomPositionSpace><tt:URI>{space}</tt:URI><tt:XRange><tt:Min>{minimum}</tt:Min><tt:Max>{maximum}</tt:Max></tt:XRange></tt:AbsoluteZoomPositionSpace></tt:Spaces>")


@pytest.fixture
def optical_settings(monkeypatch):
    settings = replace(get_settings(), camera_onvif_ptz_url="http://camera/ptz", camera_onvif_profile_token="profile-1",
        camera_onvif_ptz_configuration_token="config-1", camera_onvif_zoom_space_uri="", camera_onvif_zoom_calibration='{"1":0.1,"3":0.4}',
        camera_onvif_zoom_timeout=2, camera_onvif_zoom_tolerance=.005)
    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: settings)
    return settings


def mock_device(monkeypatch, control, positions=None, option_response=None):
    calls = []
    samples = iter(positions or [zoom_status(), zoom_status(.4)])

    def request(settings, namespace, action, body):
        calls.append((action, body))
        if action == "GetConfigurationOptions":
            return option_response if option_response is not None else options()
        if action == "GetStatus":
            return next(samples)
        if action == "GetNodes":
            return xml("<tt:ContinuousZoomVelocitySpace/>")
        if action == "GetCompatibleConfigurations":
            return xml('<t:PTZConfiguration token="config-1"/>')
        return xml("")

    monkeypatch.setattr(control, "_request", request)
    return calls


def test_calibrated_absolute_zoom_uses_real_native_positions_and_returns_to_measured_one_x(monkeypatch, optical_settings):
    control = live_capture.OnvifControl()
    calls = mock_device(monkeypatch, control, positions=[zoom_status(), zoom_status(.4), zoom_status(.4), zoom_status(.1)])
    assert control.move_to(3) == {"factor": 3, "position": .4, "settled": True}
    assert control.move_to(1) == {"factor": 1, "position": .1, "settled": True}
    movements = [body for action, body in calls if action == "AbsoluteMove"]
    assert len(movements) == 2
    assert 'x="0.4"' in movements[0] and 'x="0.1"' in movements[1]
    assert all("PanTilt" not in body for body in movements)
    assert not any(action in {"ContinuousMove", "GotoHomePosition"} for action, _ in calls)


@pytest.mark.parametrize("calibration", ["", "[]", '{"1":0}', '{"2":0.2,"3":0.4}', '{"1":0.2,"3":0.2}', '{"1":0.2,"2":0.1,"3":0.4}', '{"1":0.1,"3":"nan"}'])
def test_invalid_or_missing_magnification_calibration_is_rejected(monkeypatch, optical_settings, calibration):
    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: replace(optical_settings, camera_onvif_zoom_calibration=calibration))
    with pytest.raises(ValueError, match="标定"):
        live_capture.OnvifControl().validate_cycle(3)


def test_arbitrary_ratio_is_never_interpolated_or_treated_as_normalized_position(optical_settings):
    with pytest.raises(ValueError, match="未标定"):
        live_capture.OnvifControl().validate_cycle(2)


def test_digital_only_absolute_space_cannot_satisfy_optical_cycle(monkeypatch, optical_settings):
    control = live_capture.OnvifControl()
    calls = mock_device(monkeypatch, control, option_response=options("http://www.onvif.org/ver10/tptz/ZoomSpaces/NormalizedDigitalPositionSpace"))
    with pytest.raises(ValueError, match="绝对光学"):
        control.move_to(3)
    assert not any(action == "AbsoluteMove" for action, _ in calls)


def test_out_of_range_calibration_fails_before_moving_lens(monkeypatch, optical_settings):
    control = live_capture.OnvifControl()
    calls = mock_device(monkeypatch, control, option_response=options(maximum=.3))
    with pytest.raises(ValueError, match="超出"):
        control.move_to(3)
    assert not any(action == "AbsoluteMove" for action, _ in calls)


def test_no_absolute_position_feedback_is_rejected_before_move(monkeypatch, optical_settings):
    control = live_capture.OnvifControl()
    calls = mock_device(monkeypatch, control, positions=[xml("<t:PTZStatus/>")])
    with pytest.raises(RuntimeError, match="未返回绝对变焦位置"):
        control.move_to(3)
    assert not any(action == "AbsoluteMove" for action, _ in calls)


def test_missing_movement_status_requires_two_stable_position_reads(monkeypatch, optical_settings):
    control = live_capture.OnvifControl()
    calls = mock_device(monkeypatch, control, positions=[zoom_status(), zoom_status(.4, movement=None), zoom_status(.4, movement=None)])
    assert control.move_to(3)["settled"] is True
    assert sum(action == "GetStatus" for action, _ in calls) == 3


def test_timeout_stops_zoom_and_never_reports_success(monkeypatch, optical_settings):
    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: replace(optical_settings, camera_onvif_zoom_timeout=.01))
    control = live_capture.OnvifControl()
    calls = mock_device(monkeypatch, control, positions=[zoom_status(), zoom_status(.2, movement="MOVING")])
    with pytest.raises(RuntimeError, match="限定时间"):
        control.move_to(3)
    assert any(action == "Stop" for action, _ in calls)


def test_cancellation_stops_an_absolute_move(monkeypatch, optical_settings):
    control = live_capture.OnvifControl()
    calls = mock_device(monkeypatch, control)
    original = control._read_zoom_status
    reads = 0

    def read(settings):
        nonlocal reads
        value = original(settings)
        reads += 1
        if reads == 2:
            control.cancel_move()
        return value

    monkeypatch.setattr(control, "_read_zoom_status", read)
    with pytest.raises(RuntimeError, match="取消"):
        control.move_to(3)
    assert any(action == "Stop" for action, _ in calls)


def test_capability_reports_calibrated_exact_magnifications_without_credentials(monkeypatch, optical_settings):
    control = live_capture.OnvifControl()
    mock_device(monkeypatch, control)
    result = control.status()
    assert result["absolute_zoom_supported"] is True
    assert result["calibrated"] is True
    assert result["cycle_supported"] is True
    assert result["magnifications"] == [1, 3]
    assert "http://camera/ptz" not in str(result)


def test_profile_can_resolve_a_single_compatible_configuration(monkeypatch, optical_settings):
    monkeypatch.setattr(live_capture, "get_effective_settings", lambda: replace(optical_settings, camera_onvif_ptz_configuration_token=""))
    control = live_capture.OnvifControl()
    calls = mock_device(monkeypatch, control)
    assert control.move_to(3)["settled"] is True
    assert calls[0][0] == "GetCompatibleConfigurations"
    assert "config-1" in calls[1][1]


def test_manual_lens_actions_cannot_race_an_active_recording_cycle(monkeypatch):
    from fastapi.testclient import TestClient
    from app import api
    from app.main import app

    commands, failures, cancellations = [], [], []
    monkeypatch.setattr(api.recording_manager, "optical_control_busy", lambda: True)
    monkeypatch.setattr(api.recording_manager, "fail", failures.append)
    monkeypatch.setattr(api.onvif_control, "cancel_move", lambda: cancellations.append(True))
    monkeypatch.setattr(api.onvif_control, "command", lambda action: commands.append(action) or {"accepted": True})
    with TestClient(app) as client:
        assert client.post("/api/camera/ptz", json={"action": "zoom_in"}).status_code == 409
        assert commands == []
        assert client.post("/api/camera/ptz", json={"action": "stop"}).status_code == 200
    assert commands == ["stop"]
    assert failures and cancellations
