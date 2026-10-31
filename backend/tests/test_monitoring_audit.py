from datetime import datetime, timezone
import json

import pytest

from app import monitoring_audit as audit


@pytest.fixture
def audit_path(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    return path


def test_only_safe_operational_fields_are_written_and_timestamp_is_utc(audit_path):
    audit.record_event("camera_failed", camera_id="camera-01", session_id="abc123",
        reason="camera_read_failed", frame_count=123, enabled=True,
        password="PRIVATE_PASSWORD", username="PRIVATE_USERNAME",
        error="rtsp://PRIVATE_USER:PRIVATE_PASSWORD@host/stream",
        source_name="PRIVATE_FILE.mp4", unknown="PRIVATE_VALUE")
    value = json.loads(audit_path.read_text())
    assert set(value) == {"timestamp", "event", "camera_id", "session_id", "reason", "frame_count", "enabled"}
    assert "PRIVATE" not in audit_path.read_text()
    observed = datetime.fromisoformat(value["timestamp"])
    assert observed.utcoffset() == timezone.utc.utcoffset(observed)
    assert value["frame_count"] == 123


def test_unsafe_reason_ids_nonfinite_values_and_wrong_types_are_omitted(audit_path):
    audit.record_event("recording_settings_changed", reason="Cannot open rtsp://user:secret@host/stream",
        camera_id="http://host/camera", recording_id="a" * 81, session_id="line\nbreak",
        min_optical_zoom=float("nan"), target_height_ratio=float("inf"), zoom_factor=-1,
        enabled=1, frame_count=True, generation=-1, phase="private message",
        recording_mode="anything", source_type="file://private")
    value = json.loads(audit_path.read_text())
    assert set(value) == {"timestamp", "event"}
    audit.record_event("zoom_observation", zoom_factor=None, generation=3, phase="overview")
    assert audit.recent_events()[-1]["zoom_factor"] is None


def test_unknown_events_do_not_create_a_log(audit_path):
    audit.record_event("private-error-text", reason="error")
    audit.record_event(["invalid-type"])
    assert not audit_path.exists()


def test_rotation_keeps_only_bounded_own_sibling_backups(audit_path, monkeypatch):
    monkeypatch.setattr(audit, "MAX_BYTES", 150)
    for index in range(10):
        audit.record_event("camera_running", frame_count=index)
    assert sorted(path.name for path in audit_path.parent.iterdir()) == [
        "events.jsonl", "events.jsonl.1", "events.jsonl.2", "events.jsonl.3"]
    assert all(path.stat().st_size <= 150 for path in audit_path.parent.iterdir())
    assert audit.recent_events()[-1]["frame_count"] == 9


def test_disk_or_rotation_failure_never_interrupts_the_caller(audit_path, monkeypatch):
    audit.record_event("camera_running")
    monkeypatch.setattr(audit, "MAX_BYTES", 1)
    def fail(_path):
        raise PermissionError("do not publish this raw error")
    monkeypatch.setattr(audit, "_rotate", fail)
    audit.record_event("camera_failed", reason="capture_failed")
    assert len(audit.recent_events()) == 1
    audit_path.unlink()
    audit_path.mkdir()
    audit.record_event("camera_running")
    assert audit.recent_events() == []


def test_reader_bounds_output_and_revalidates_stored_content(audit_path, monkeypatch):
    for index in range(120):
        audit.record_event("camera_running", frame_count=index)
    assert len(audit.recent_events(1000)) == 100
    assert [item["frame_count"] for item in audit.recent_events(2)] == [118, 119]
    with audit_path.open("a", encoding="utf-8") as stream:
        stream.write('invalid-json\n')
        stream.write(json.dumps({"event": "camera_failed", "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": "rtsp://secret@host", "password": "never return", "frame_count": 121}) + "\n")
        stream.write(json.dumps({"event": "camera_failed", "timestamp": "2026-10-31T00:00:00"}) + "\n")
    value = audit.recent_events(1)[0]
    assert value["frame_count"] == 121
    assert "reason" not in value and "password" not in value
    monkeypatch.setattr(audit, "TAIL_BYTES", 300)
    assert len(audit.recent_events(100)) <= 2
