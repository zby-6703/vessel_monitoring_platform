from dataclasses import replace
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import api
from app.camera_profiles import CameraProfileService
from app.camera_setup import CameraSetupError
from app.config import Settings


def discovered(token="aux"):
    return {"env_values": {"LIVE_CAPTURE_URL": "rtsp://operator:secret@camera:554/aux", "CAMERA_ONVIF_PROFILE_TOKEN": token,
        "CAMERA_ONVIF_SOURCE_TOKEN": "source", "CAMERA_ONVIF_PTZ_CONFIGURATION_TOKEN": "ptz-config", "CAMERA_ONVIF_PTZ_URL": "http://camera/ptz",
        "CAMERA_ONVIF_IMAGING_URL": "http://camera/imaging", "LIVE_CAPTURE_FPS_OVERRIDE": "12"},
        "summary": {"profiles": [{"token": token, "name": "Auxiliary", "width": 1280, "height": 720}], "device": {"model": "test"}, "warnings": []}}


def test_select_profile_persists_private_uri_and_matching_tokens_only_on_server(tmp_path, monkeypatch):
    service = CameraProfileService(tmp_path / "profile.json")
    base = replace(Settings(), live_capture_url="rtsp://old", camera_onvif_source_token="source", live_stream_url="http://relay/main.flv")
    monkeypatch.setattr(service, "discover", lambda settings, token: discovered(token))
    public = service.select(base, "aux")
    assert "secret" not in json.dumps(public) and "rtsp://" not in json.dumps(public)
    saved = service.effective(base)
    assert saved.live_capture_url.endswith("/aux")
    assert saved.camera_onvif_profile_token == "aux"
    assert saved.camera_onvif_source_token == "source"
    assert saved.camera_onvif_ptz_configuration_token == "ptz-config"
    assert saved.live_capture_fps_override == 12
    assert saved.live_stream_url == ""
    assert CameraProfileService(service.path).effective(base) == saved
    changed = replace(base, camera_onvif_password="changed")
    assert service.effective(changed) == changed


def test_failed_profile_lookup_keeps_last_selection(tmp_path, monkeypatch):
    service = CameraProfileService(tmp_path / "profile.json")
    base = replace(Settings(), camera_onvif_source_token="source")
    monkeypatch.setattr(service, "discover", lambda settings, token: discovered(token))
    service.select(base, "aux")
    before = service.path.read_bytes()
    def fail(*args): raise CameraSetupError("unknown profile")
    monkeypatch.setattr(service, "discover", fail)
    with pytest.raises(CameraSetupError): service.select(base, "missing")
    assert service.path.read_bytes() == before


def test_another_physical_source_cannot_reuse_previous_zoom_calibration(tmp_path, monkeypatch):
    service = CameraProfileService(tmp_path / "profile.json")
    base = replace(Settings(), camera_onvif_source_token="other-lens")
    monkeypatch.setattr(service, "discover", lambda settings, token: discovered(token))
    with pytest.raises(CameraSetupError, match="另一视频源"):
        service.select(base, "aux")
    assert not service.path.exists()


def test_channel_switch_is_rejected_while_capture_or_motor_is_busy(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setattr(api.camera_capture, "is_alive", lambda: True)
    monkeypatch.setattr(api.camera_profile_service, "select", lambda *_: pytest.fail("must not change a running source"))
    with TestClient(app) as client:
        assert client.put("/api/camera/profiles", json={"profile_token": "aux"}).status_code == 409
        assert client.put("/api/camera/profiles", json={"profile_token": ""}).status_code == 422


def test_idle_profile_selection_invalidates_previous_lens_observation(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setattr(api.camera_capture, "is_alive", lambda: False)
    monkeypatch.setattr(api.local_playback, "is_alive", lambda: False)
    monkeypatch.setattr(api.recording_manager, "optical_control_busy", lambda: False)
    events = []
    monkeypatch.setattr(api.camera_profile_service, "select", lambda _base, token: {"selected_profile": token})
    monkeypatch.setattr(api.onvif_control, "close", lambda: events.append("close"))
    monkeypatch.setattr(api.recording_manager, "invalidate_zoom_observation", lambda: events.append("invalidate"))
    with TestClient(app) as client:
        assert client.put("/api/camera/profiles", json={"profile_token": "aux"}).json() == {"selected_profile": "aux"}
    assert events == ["close", "invalidate"]
