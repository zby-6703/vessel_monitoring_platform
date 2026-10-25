from __future__ import annotations

import json
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import recording_api
from app import recording as recording_module
from app.recording import RecordingManager


@pytest.fixture
def recording_client(tmp_path, monkeypatch):
    manager = RecordingManager(tmp_path)
    monkeypatch.setattr(recording_api, "recording_manager", manager)
    app = FastAPI()
    app.include_router(recording_api.router)
    with TestClient(app) as client:
        yield client, manager
    manager.finish()
    manager.executor.shutdown(wait=True)


def create_event(manager: RecordingManager) -> str:
    manager.update_settings({"enabled": True, "min_hits": 1, "pre_roll_seconds": 0})
    image = np.full((80, 128, 3), 100, dtype=np.uint8)
    manager.process_frame(
        image,
        camera_id="api-camera",
        session_id="api-session",
        source_name="api-test.mp4",
        fps=10,
        timestamp=0,
        vessels=[{
            "ship_id": 42,
            "ship_name": "海泰52",
            "confidence": .9,
            "boxes": [{"type": "ship", "xywh": [20, 10, 80, 60], "confidence": .9}],
        }],
    )
    manager.finish()
    manager.executor.submit(lambda: None).result(timeout=30)
    return manager.list_recordings()["items"][0]["id"]


def test_recording_routes_are_registered_in_main_app():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert {"/api/recording/settings", "/api/recording/status", "/api/recordings"} <= paths
    assert any(path.startswith("/api/recordings/{") and path.endswith("}") for path in paths)


def test_settings_and_status_are_available_and_partial_settings_persist(recording_client):
    client, manager = recording_client
    response = client.get("/api/recording/settings")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    saved = client.put("/api/recording/settings", json={"enabled": True, "zoom_factor": 3, "focus_mode": "manual"})
    assert saved.status_code == 200
    assert manager.get_settings()["enabled"] is True
    assert manager.get_settings()["zoom_factor"] == 3
    second = client.put("/api/recording/settings", json={"center_x": .25})
    assert second.status_code == 200
    assert second.json()["enabled"] is True
    assert second.json()["zoom_factor"] == 3
    assert second.json()["focus_mode"] == "manual"
    status = client.get("/api/recording/status")
    assert status.status_code == 200
    assert isinstance(status.json(), dict)


@pytest.mark.parametrize("values", [
    {"pre_roll_seconds": -1},
    {"confidence_threshold": 2},
    {"min_hits": 0},
    {"focus_mode": "invalid"},
    {"center_x": 2},
])
def test_invalid_settings_return_422_without_mutating_settings(recording_client, values):
    client, manager = recording_client
    original = manager.get_settings()
    assert client.put("/api/recording/settings", json=values).status_code == 422
    assert manager.get_settings() == original


def test_catalog_filters_media_ranges_and_delete(recording_client):
    client, manager = recording_client
    recording_id = create_event(manager)
    response = client.get("/api/recordings", params={"query": "海泰52", "camera_id": "api-camera", "page": 1, "page_size": 1})
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == recording_id
    assert client.get("/api/recordings", params={"query": "不存在"}).json()["items"] == []
    assert client.get("/api/recordings", params={"camera_id": "other-camera"}).json()["items"] == []

    for kind, content_type in [("original", "video/mp4"), ("focused", "video/mp4"), ("thumbnail", "image/jpeg")]:
        media = client.get(f"/api/recordings/{recording_id}/{kind}")
        assert media.status_code == 200
        assert media.headers["content-type"].startswith(content_type)
        assert len(media.content) > 0
    partial = client.get(f"/api/recordings/{recording_id}/original", headers={"Range": "bytes=0-31"})
    assert partial.status_code == 206
    assert len(partial.content) == 32
    assert partial.headers["content-range"].startswith("bytes 0-31/")

    deleted = client.delete(f"/api/recordings/{recording_id}")
    assert deleted.status_code in {200, 204}
    assert client.get("/api/recordings").json()["items"] == []
    assert client.get(f"/api/recordings/{recording_id}/original").status_code == 404
    assert client.delete(f"/api/recordings/{recording_id}").status_code == 404


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 10000}, {"date_from": "not-a-date"}])
def test_catalog_rejects_invalid_pagination_and_dates(recording_client, params):
    client, _ = recording_client
    assert client.get("/api/recordings", params=params).status_code == 422


def test_deleting_busy_media_returns_conflict_and_keeps_event_index(recording_client, monkeypatch):
    client, manager = recording_client
    recording_id = "a" * 32
    item = {"id": recording_id, "status": "completed"}
    manager.catalog[recording_id] = item
    directory = manager.root / recording_id
    directory.mkdir()
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(item), encoding="utf-8")

    def busy_delete(_):
        manifest.unlink(missing_ok=True)
        raise PermissionError("a client is downloading the video")

    monkeypatch.setattr(recording_module.shutil, "rmtree", busy_delete)
    response = client.delete(f"/api/recordings/{recording_id}")
    assert response.status_code == 409
    assert recording_id in manager.catalog
    assert json.loads(manifest.read_text(encoding="utf-8"))["id"] == recording_id
