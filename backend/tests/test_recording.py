from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import shutil
import subprocess
from types import SimpleNamespace
import uuid

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from app import recording as recording_module
from app.recording import RecordingManager, RecordingSettings, focus_rectangle


@pytest.fixture(autouse=True)
def close_test_managers(monkeypatch):
    original = RecordingManager.__init__
    created = []

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(RecordingManager, "__init__", initialize)
    yield
    for manager in created:
        manager.finish()
        manager.executor.shutdown(wait=True)


def wait_for_encoding(manager: RecordingManager) -> None:
    manager.executor.submit(lambda: None).result(timeout=30)


def catalog_item(root: Path, *, days_ago: int = 0, status: str = "completed") -> dict:
    recording_id = uuid.uuid4().hex
    timestamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    item = {
        "id": recording_id, "camera_id": "test-camera", "source_name": "test.mp4", "session_id": "test-session",
        "started_at": timestamp, "ended_at": timestamp, "duration_seconds": 1, "status": status,
        "ship_names": [], "track_ids": [], "bytes": 0, "reason": None, "error": None,
    }
    directory = root / recording_id
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(item), encoding="utf-8")
    return item


def vessel(confidence: float = .9, name: str = "海泰52", track_id: int = 1) -> dict:
    return {
        "ship_id": track_id,
        "ship_name": name,
        "confidence": confidence,
        "boxes": [
            {"type": "ship", "xywh": [24, 16, 80, 48], "confidence": confidence},
            {"type": "name", "xywh": [30, 20, 20, 12], "confidence": .8},
            {"type": "waterline_area", "xywh": [60, 48, 30, 12], "confidence": .8},
        ],
    }


def frame(value: int = 60) -> np.ndarray:
    image = np.full((80, 128, 3), value, dtype=np.uint8)
    image[16:64, 24:104] = (30, 120, 220)
    return image


def feed(manager: RecordingManager, timestamp: float, vessels=None, **source) -> None:
    manager.process_frame(
        frame(int(timestamp * 10) % 200),
        camera_id=source.get("camera_id", "camera-test"),
        session_id=source.get("session_id", "session-test"),
        source_name="test.mp4",
        fps=10,
        timestamp=timestamp,
        vessels=vessels,
    )


def enabled_manager(tmp_path: Path, **overrides) -> RecordingManager:
    manager = RecordingManager(tmp_path)
    manager.update_settings({"enabled": True, "min_hits": 1, "pre_roll_seconds": 0, "post_roll_seconds": 1, **overrides})
    return manager


def decode(path: Path) -> list[np.ndarray]:
    capture = cv2.VideoCapture(str(path))
    assert capture.isOpened(), f"saved recording is not decodable: {path}"
    frames = []
    try:
        while True:
            ok, image = capture.read()
            if not ok:
                break
            frames.append(image)
    finally:
        capture.release()
    assert frames, f"saved recording has no frames: {path}"
    return frames


def test_settings_are_opt_in_validated_and_persisted(tmp_path: Path):
    manager = RecordingManager(tmp_path)
    assert manager.get_settings()["enabled"] is False
    manager.update_settings({"enabled": True, "focus_mode": "manual", "zoom_factor": 3, "center_x": .25})
    manager.update_settings({"center_y": .75})
    restored = RecordingManager(tmp_path).get_settings()
    assert restored["enabled"] is True
    assert restored["focus_mode"] == "manual"
    assert restored["zoom_factor"] == 3
    assert restored["center_x"] == .25
    assert restored["center_y"] == .75


@pytest.mark.parametrize("values", [
    {"pre_roll_seconds": -1},
    {"post_roll_seconds": -1},
    {"min_hits": 0},
    {"confidence_threshold": 1.1},
    {"zoom_factor": .5},
    {"center_x": 1.1},
    {"center_y": -.1},
    {"focus_mode": "unsupported"},
])
def test_settings_reject_invalid_recording_and_focus_values(values):
    with pytest.raises(ValidationError):
        RecordingSettings(**values)


def test_disabled_and_unconfirmed_detections_create_no_recordings(tmp_path: Path):
    manager = RecordingManager(tmp_path)
    feed(manager, 0, [vessel()])
    manager.finish()
    assert manager.list_recordings()["items"] == []

    manager.update_settings({"enabled": True, "min_hits": 2, "confidence_threshold": .7})
    feed(manager, 0, [vessel(.5)])
    feed(manager, .1, [vessel()])
    for index in range(2, 8):
        feed(manager, index / 10, None)
    feed(manager, .8, [])
    manager.finish()
    assert manager.list_recordings()["items"] == []


def test_event_saves_original_focused_and_thumbnail_with_full_frame_rate(tmp_path: Path):
    manager = enabled_manager(tmp_path, pre_roll_seconds=.3, post_roll_seconds=1, min_hits=2)
    for index in range(4):
        feed(manager, index / 10, [])
    feed(manager, .4, [vessel()])
    for timestamp in [.5, .6]:
        feed(manager, timestamp, None)
    feed(manager, .7, [vessel()])
    for index in range(8, 20):
        feed(manager, index / 10, [])
    manager.finish()
    wait_for_encoding(manager)

    items = manager.list_recordings()["items"]
    assert len(items) == 1
    # Confirmation does not discard the first detection's preceding buffer.
    assert items[0]["source_start_seconds"] <= .21
    recording_id = items[0]["id"]
    original = decode(manager.media_path(recording_id, "original"))
    focused = decode(manager.media_path(recording_id, "focused"))
    # Record all source frames, including frames that did not run inference.
    assert len(original) >= 7
    assert len(original) == len(focused)
    assert original[0].shape[:2] == (80, 128)
    thumbnail = cv2.imread(str(manager.media_path(recording_id, "thumbnail")))
    assert thumbnail is not None and thumbnail.size > 0
    assert RecordingManager(tmp_path).list_recordings()["items"][0]["id"] == recording_id
    if ffprobe := shutil.which("ffprobe"):
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "json", str(manager.media_path(recording_id, "original"))],
            check=True, capture_output=True, text=True, timeout=10,
        )
        assert json.loads(probe.stdout)["streams"][0]["codec_name"] == "h264"


def test_disabling_recording_finalizes_current_event(tmp_path: Path):
    manager = enabled_manager(tmp_path)
    feed(manager, 0, [vessel()])
    feed(manager, .1, None)
    manager.update_settings({"enabled": False})
    assert len(manager.list_recordings()["items"]) == 1
    feed(manager, .2, [vessel()])
    manager.finish()
    assert len(manager.list_recordings()["items"]) == 1


def test_source_switch_keeps_events_and_preroll_separate(tmp_path: Path):
    manager = enabled_manager(tmp_path, pre_roll_seconds=.5)
    feed(manager, 0, [vessel(name="第一艘")], camera_id="camera-a", session_id="session-a")
    feed(manager, .1, None, camera_id="camera-a", session_id="session-a")
    feed(manager, 0, [], camera_id="camera-b", session_id="session-b")
    feed(manager, .1, [vessel(name="第二艘")], camera_id="camera-b", session_id="session-b")
    manager.finish()
    items = manager.list_recordings()["items"]
    assert len(items) == 2
    assert {item["camera_id"] for item in items} == {"camera-a", "camera-b"}
    assert len(manager.list_recordings(camera_id="camera-a")["items"]) == 1


def test_delete_removes_all_event_media_and_missing_ids_are_rejected(tmp_path: Path):
    manager = enabled_manager(tmp_path)
    feed(manager, 0, [vessel()])
    manager.finish()
    wait_for_encoding(manager)
    recording_id = manager.list_recordings()["items"][0]["id"]
    paths = [manager.media_path(recording_id, kind) for kind in ["original", "focused", "thumbnail"]]
    manager.delete_recording(recording_id)
    assert manager.list_recordings()["items"] == []
    assert all(not path.exists() for path in paths)
    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        manager.media_path("../../settings", "original")
    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        manager.media_path(recording_id, "original")


def test_auto_focus_encloses_multiple_vessels_and_recognition_regions():
    vessels = [
        {"boxes": [
            {"type": "ship", "xywh": [100, 100, 200, 100]},
            {"type": "ship_name_area", "xywh": [90, 95, 30, 20]},
            {"type": "waterline_area", "xywh": [290, 190, 40, 30]},
        ]},
        {"boxes": [{"type": "ship", "xywh": [600, 350, 200, 100]}]},
    ]
    rectangle = focus_rectangle(1000, 600, vessels, RecordingSettings(zoom_factor=8, padding=0))
    assert rectangle["x"] <= .09
    assert rectangle["y"] <= 95 / 600
    assert rectangle["x"] + rectangle["width"] >= .8
    assert rectangle["y"] + rectangle["height"] >= .75
    assert rectangle["width"] == rectangle["height"]


def test_manual_focus_clamps_to_source_edges():
    rectangle = focus_rectangle(1000, 600, [], RecordingSettings(focus_mode="manual", zoom_factor=4, center_x=0, center_y=1))
    assert rectangle == {"x": 0, "y": .75, "width": .25, "height": .25}


def test_local_preview_of_an_optical_setting_uses_bounded_digital_zoom(tmp_path: Path):
    manager = enabled_manager(tmp_path, recording_mode="optical_cycle", zoom_factor=40, focus_mode="manual")
    feed(manager, 0, [vessel()])
    assert manager.focus["width"] == pytest.approx(1 / 8)
    item = manager.list_recordings()["items"][0]
    assert item["recording_mode"] == "passage"
    assert item["settings"]["zoom_factor"] == 8
    assert item["settings"]["recording_mode"] == "passage"
    assert manager.current_zoom_factor is None


def test_long_passage_is_split_without_losing_source_frames(tmp_path: Path):
    manager = enabled_manager(tmp_path, max_clip_seconds=5)
    for index in range(111):
        feed(manager, index / 10, [vessel()])
    manager.finish()
    wait_for_encoding(manager)
    items = manager.list_recordings()["items"]
    assert len(items) == 3
    assert sum(len(decode(manager.media_path(item["id"], "original"))) for item in items) == 111
    assert all(item["duration_seconds"] <= 5 for item in items)


def test_retention_removes_expired_completed_events_but_keeps_processing_events(tmp_path: Path):
    manager = RecordingManager(tmp_path)
    expired = catalog_item(tmp_path, days_ago=31)
    recent = catalog_item(tmp_path, days_ago=1)
    processing = catalog_item(tmp_path, days_ago=31, status="processing")
    manager.catalog.update({item["id"]: item for item in [expired, recent, processing]})
    manager.status()
    assert expired["id"] not in manager.catalog
    assert not (tmp_path / expired["id"]).exists()
    assert set(manager.catalog) == {recent["id"], processing["id"]}


def test_storage_quota_removes_oldest_completed_events_first(tmp_path: Path, monkeypatch):
    manager = RecordingManager(tmp_path)
    items = [catalog_item(tmp_path, days_ago=days) for days in [3, 2, 1]]
    manager.catalog.update({item["id"]: item for item in items})
    # Model large media without allocating hundreds of megabytes for the test.
    monkeypatch.setattr(manager, "_storage_bytes", lambda: len(manager.catalog) * 40 * 1024**2)
    manager.update_settings({"max_storage_gb": .1})
    assert set(manager.catalog) == {items[1]["id"], items[2]["id"]}
    assert not (tmp_path / items[0]["id"]).exists()


def test_busy_windows_media_does_not_interrupt_capture_and_preserves_manifest(tmp_path: Path, monkeypatch):
    manager = enabled_manager(tmp_path)
    expired = catalog_item(tmp_path, days_ago=31)
    manager.catalog[expired["id"]] = expired
    manifest = tmp_path / expired["id"] / "manifest.json"

    def busy_delete(path):
        assert Path(path).name == expired["id"]
        # Windows rmtree can delete the manifest before encountering an open MP4.
        manifest.unlink(missing_ok=True)
        raise PermissionError("video is open in another process")

    class Writer:
        def __init__(self, *_): pass
        def write(self, *_): pass
        def close(self): pass

    monkeypatch.setattr(recording_module.shutil, "rmtree", busy_delete)
    monkeypatch.setattr(recording_module, "ClipWriter", Writer)
    monkeypatch.setattr(manager, "_encode", lambda *_: None)
    manager.last_maintenance = 0
    assert manager.status()["state"] == "armed"
    assert json.loads(manifest.read_text(encoding="utf-8"))["id"] == expired["id"]
    manager.last_maintenance = 0
    feed(manager, 0, [vessel()])
    assert manager.status()["state"] == "recording"
    assert manager.status()["error"] is None
    with pytest.raises(ValueError, match="正在使用|无法删除"):
        manager.delete_recording(expired["id"])
    assert expired["id"] in manager.catalog
    assert manifest.is_file()


def test_encoding_failure_is_visible_and_never_exposes_playable_media(tmp_path: Path, monkeypatch):
    manager = RecordingManager(tmp_path)
    item = catalog_item(tmp_path, status="processing")
    manager.catalog[item["id"]] = item
    directory = tmp_path / item["id"]
    (directory / "original.capture.mp4").write_bytes(b"incomplete capture")
    (directory / "original.mp4").write_bytes(b"incomplete output")
    monkeypatch.setattr(recording_module.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(recording_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    manager._encode(item["id"], "completed")
    result = manager.list_recordings()["items"][0]
    assert result["status"] == "failed"
    assert result["error"]
    assert result["original_url"] is None
    assert result["focused_url"] is None
    assert json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["status"] == "failed"
    with pytest.raises(FileNotFoundError):
        manager.media_path(item["id"], "original")


def test_recovery_workers_observe_the_complete_catalog_before_running_cleanup(tmp_path: Path, monkeypatch):
    items = [catalog_item(tmp_path, status=status) for status in ["recording", "processing"]]
    expected = {item["id"] for item in items}
    snapshots = []

    class ImmediateExecutor:
        def __init__(self, **_): pass
        def submit(self, function, *args): return function(*args)
        def shutdown(self, **_): pass

    def recover(manager, recording_id, final_status):
        snapshots.append(set(manager.catalog))
        manager.catalog[recording_id]["status"] = final_status
        manager._maintenance(force=True)

    # Execute workers immediately to make an initialization race deterministic.
    monkeypatch.setattr(recording_module, "ThreadPoolExecutor", ImmediateExecutor)
    monkeypatch.setattr(RecordingManager, "_encode", recover)
    manager = RecordingManager(tmp_path)
    assert len(snapshots) == 2
    assert all(snapshot == expected for snapshot in snapshots)
    assert {item["status"] for item in manager.catalog.values()} == {"interrupted"}
