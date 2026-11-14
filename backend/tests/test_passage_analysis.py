from dataclasses import replace
from datetime import datetime, timezone
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.jobs import JobManager
from app.recording import RecordingManager, _atomic_json
from app import api, recording_api


@pytest.fixture
def archive(tmp_path):
    recording = RecordingManager(tmp_path / "passages")
    jobs = JobManager(replace(Settings(), result_root=tmp_path / "results", upload_root=tmp_path / "uploads"))
    jobs.bind_recordings(recording)
    recording_id = "a" * 32
    directory = recording.root / recording_id
    directory.mkdir()
    (directory / "cycle.mp4").write_bytes(b"test immutable video")
    item = {"id": recording_id, "status": "completed", "recording_mode": "optical_cycle", "started_at": datetime.now(timezone.utc).isoformat(),
            "camera_id": "river", "source_name": "river camera", "ship_names": [], "track_ids": [7], "bytes": 20}
    recording.catalog[recording_id] = item
    _atomic_json(directory / "manifest.json", item)
    yield recording, jobs, recording_id
    jobs.stop()
    recording.executor.shutdown(wait=True)
    recording.control_executor.shutdown(wait=True)


class GoodPipeline:
    def process_file(self, source, **kwargs):
        assert source.read_bytes() == b"test immutable video"
        assert kwargs["task"].value == "full_pipeline" and kwargs["enable_tracking"]
        assert kwargs["max_frames"] is None
        kwargs["progress"](20, "processed frame 20")
        return {"result_uri": "result.jsonl", "visual_uri": None, "processed_samples": 24,
                "csv_rows": [{"instance_id": 1, "recognized_zh": "测试船88", "recognized_en": "TEST88", "draft_depth_m": 2.6, "status": "pending_review"}]}


def test_recording_analysis_is_full_tracked_idempotent_and_persistent(archive):
    recording, jobs, rid = archive
    first = jobs.create_from_recording(recording, rid, {"frame_step": 1, "visualize": False})
    assert first["source_recording"]["id"] == rid
    assert jobs.create_from_recording(recording, rid, {})["id"] == first["id"]
    assert recording.catalog[rid]["analysis"]["status"] == "queued"
    with pytest.raises(ValueError, match="离线分析"):
        recording.delete_recording(rid)
    jobs._pipeline = lambda storage: GoodPipeline()
    jobs._run(first["id"])
    item = recording.catalog[rid]
    assert item["analysis"]["status"] == "completed"
    assert item["ship_names"] == ["测试船88"]
    assert item["analysis_result"]["draft_depths_m"] == [2.6]
    assert jobs.get(first["id"])["items"][0]["processed_samples"] == 24
    restored = RecordingManager(recording.root)
    try:
        assert restored.catalog[rid]["analysis_result"]["job_id"] == first["id"]
    finally:
        restored.executor.shutdown(wait=True)
        restored.control_executor.shutdown(wait=True)
    # Analysis owns a hardlink/copy, so source playback survives retention.
    recording.delete_recording(rid)
    assert (jobs.settings.upload_root / first["id"] / first["items"][0]["upload_name"]).is_file()


def test_failed_retry_retains_previous_success_and_cancel_is_retryable(archive):
    recording, jobs, rid = archive
    first = jobs.create_from_recording(recording, rid, {})
    jobs._pipeline = lambda storage: GoodPipeline()
    jobs._run(first["id"])
    second = jobs.create_from_recording(recording, rid, {})
    class Failed:
        def process_file(self, *_args, **_kwargs): raise RuntimeError("model unavailable")
    jobs._pipeline = lambda storage: Failed()
    jobs._run(second["id"])
    assert recording.catalog[rid]["analysis"]["status"] == "failed"
    assert recording.catalog[rid]["analysis_result"]["job_id"] == first["id"]
    third = jobs.create_from_recording(recording, rid, {})
    jobs.cancel(third["id"])
    assert recording.catalog[rid]["analysis"]["status"] == "cancelled"
    assert jobs.create_from_recording(recording, rid, {})["id"] != third["id"]


def test_running_cancel_does_not_report_model_failure(archive):
    recording, jobs, rid = archive
    job = jobs.create_from_recording(recording, rid, {})
    class Cancelled:
        def process_file(self, *_args, **_kwargs): raise InterruptedError()
    jobs._pipeline = lambda storage: Cancelled()
    jobs._run(job["id"])
    assert jobs.get(job["id"])["status"] == "cancelled"
    assert recording.catalog[rid]["analysis"]["status"] == "cancelled"


def test_restart_reconciles_active_analysis_to_failed(archive):
    recording, jobs, rid = archive
    job = jobs.create_from_recording(recording, rid, {})
    reloaded = JobManager(jobs.settings)
    reloaded.bind_recordings(recording)
    assert reloaded.get(job["id"])["status"] == "failed"
    assert recording.catalog[rid]["analysis"]["status"] == "failed"
    reloaded.stop()


def test_missing_job_does_not_permanently_lock_archive(archive):
    recording, jobs, rid = archive
    recording.catalog[rid]["analysis"] = {"id": "missing", "status": "running"}
    jobs.bind_recordings(recording)
    assert recording.catalog[rid]["analysis"]["status"] == "failed"
    assert "未找到" in recording.catalog[rid]["analysis"]["error"]
    assert jobs.create_from_recording(recording, rid, {})["status"] == "queued"


def test_analysis_endpoint_and_source_endpoint_work_without_upload(archive, monkeypatch):
    recording, jobs, rid = archive
    monkeypatch.setattr(recording_api, "recording_manager", recording)
    monkeypatch.setattr(recording_api, "job_manager", jobs)
    monkeypatch.setattr(api, "job_manager", jobs)
    app = FastAPI()
    app.include_router(recording_api.router)
    app.include_router(api.router)
    with TestClient(app) as client:
        response = client.post(f"/api/recordings/{rid}/analyze", json={"frame_step": 2, "visualize": False})
        assert response.status_code == 202
        job = response.json()
        assert client.get(f"/api/recordings/{rid}").json()["analysis"]["id"] == job["id"]
        assert client.get(f"/api/jobs/{job['id']}/source/1").content == b"test immutable video"
        assert client.get(f"/api/jobs/{job['id']}/source/0").status_code == 404
        assert client.post(f"/api/recordings/{rid}/analyze", json={"frame_step": 0}).status_code == 422
        assert client.post("/api/recordings/missing/analyze").status_code == 404
