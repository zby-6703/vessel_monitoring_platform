"""Manual calibrated lens commands never compete with automatic ownership."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import api
from app.recording import RecordingManager, RecordingSettings


@pytest.fixture
def manual_client(tmp_path, monkeypatch):
    manager = RecordingManager(tmp_path)
    calls = []
    def move(factor):
        assert manager.optical_control_busy() and manager.status()["manual_zoom_active"]
        assert not manager.zoom_observation_context()["accept_detections"]
        calls.append(factor)
        return {"factor": factor, "settled": True}
    lens = SimpleNamespace(available_magnifications=lambda: [1, 2, 3, 4, 6, 8], move_to=move,
        cancel_move=lambda: calls.append("cancel"), command=lambda action: {"accepted": True, "action": action})
    camera = SimpleNamespace(is_alive=lambda: False)
    monkeypatch.setattr(api, "recording_manager", manager)
    monkeypatch.setattr(api, "onvif_control", lens)
    monkeypatch.setattr(api, "camera_capture", camera)
    monkeypatch.setattr(api.detail_focus, "busy", lambda: False)
    monkeypatch.setattr(api.detail_focus, "stop", lambda: None)
    application = FastAPI()
    application.include_router(api.router)
    with TestClient(application) as client:
        yield client, manager, lens, camera, calls
    manager.executor.shutdown(wait=True)
    manager.control_executor.shutdown(wait=True)


@pytest.mark.parametrize("factor", [1, 2, 6, 8])
def test_exact_calibrated_zoom_records_only_acknowledged_factor(manual_client, factor):
    client, manager, _, _, calls = manual_client
    previous = manager.settings.model_dump()
    response = client.post("/api/camera/zoom", json={"factor": factor})
    assert response.status_code == 200 and response.json() == {"factor": factor, "settled": True}
    assert calls == [factor] and manager.current_zoom_factor == factor
    assert not manager.optical_control_busy() and not manager.status()["manual_zoom_active"]
    assert manager.settings.model_dump() == previous and not manager.settings_path.exists()


@pytest.mark.parametrize("factor,code", [(0, 422), (41, 422), ("NaN", 422), ("Infinity", 422), (5, 409)])
def test_unmarked_or_invalid_factor_never_dispatches(manual_client, factor, code):
    client, manager, _, _, calls = manual_client
    manager.observe_zoom_factor(2)
    response = client.post("/api/camera/zoom", json={"factor": factor})
    assert response.status_code == code and calls == []
    assert manager.current_zoom_factor == 2


def test_automatic_source_owns_lens_even_before_first_cycle_frame(manual_client):
    client, manager, _, camera, calls = manual_client
    manager.settings = RecordingSettings(enabled=True)
    camera.is_alive = lambda: True
    assert client.post("/api/camera/zoom", json={"factor": 3}).status_code == 409
    assert not calls and not manager.status()["manual_zoom_active"]


def test_an_outstanding_automatic_return_blocks_manual_zoom(manual_client):
    client, manager, _, _, calls = manual_client
    manager.settings = RecordingSettings(enabled=True)
    manager._optical_session = True
    assert client.post("/api/camera/zoom", json={"factor": 3}).status_code == 409
    assert not calls


def test_manual_zoom_is_allowed_with_live_preview_and_automatic_recording_off(manual_client):
    client, manager, _, camera, calls = manual_client
    camera.is_alive = lambda: True
    assert not manager.settings.enabled
    assert client.post("/api/camera/zoom", json={"factor": 4}).status_code == 200
    assert calls == [4] and manager.current_zoom_factor == 4


@pytest.mark.parametrize("result", [{"factor": 3, "settled": False}, {"factor": 2, "settled": True}])
def test_unconfirmed_native_move_keeps_zoom_unknown(manual_client, result):
    client, manager, lens, _, _ = manual_client
    lens.move_to = lambda _: result
    assert client.post("/api/camera/zoom", json={"factor": 3}).status_code == 502
    assert manager.current_zoom_factor is None and not manager.status()["manual_zoom_active"]


def test_transport_failure_releases_manual_reservation_without_claiming_success(manual_client):
    client, manager, lens, _, _ = manual_client
    def fail(_): raise TimeoutError("simulated unavailable camera")
    lens.move_to = fail
    assert client.post("/api/camera/zoom", json={"factor": 3}).status_code == 502
    assert manager.current_zoom_factor is None and not manager.optical_control_busy()


def test_manual_reservation_blocks_another_zoom_source_start_and_focus(manual_client):
    client, manager, _, _, calls = manual_client
    generation = manager.begin_manual_zoom()
    try:
        assert client.post("/api/camera/zoom", json={"factor": 3}).status_code == 409
        assert client.post("/api/realtime/camera/start").status_code == 409
        assert client.post("/api/camera/focus", json={"left": .2, "top": .2, "right": .5, "bottom": .5}).status_code == 409
        assert not calls
        assert client.post("/api/camera/ptz", json={"action": "stop"}).status_code == 200
        assert calls == ["cancel"] and manager.error is None
    finally:
        manager.end_manual_zoom(None, generation)
