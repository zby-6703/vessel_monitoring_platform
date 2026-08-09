from fastapi.testclient import TestClient

from app.main import app


def test_health_and_new_jobs_route_are_available():
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        response = client.get("/api/jobs")
        assert response.status_code == 200
        assert isinstance(response.json()["items"], list)
        dashboard = client.get("/api/dashboard")
        assert dashboard.status_code == 200
        assert {"statistics", "records"} <= dashboard.json().keys()


def test_new_jobs_route_rejects_unsupported_task_and_media():
    with TestClient(app) as client:
        task = client.post("/api/jobs", data={"task": "tracking"}, files=[("files", ("sample.jpg", b"image", "image/jpeg"))])
        assert task.status_code == 422
        media = client.post("/api/jobs", files=[("files", ("notes.txt", b"data", "text/plain"))])
        assert media.status_code == 415


def test_realtime_event_requires_explicit_current_fields():
    with TestClient(app) as client:
        assert client.post("/api/realtime/instances", json={}).status_code == 422
        assert client.post("/api/realtime/instances", json={"track_id": 1, "current_name": "UNKNOWN", "current_draft_depth_m": None, "current_status": "pending_review"}).status_code == 202
