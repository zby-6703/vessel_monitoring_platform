from dataclasses import replace
import importlib.util
import json
from pathlib import Path

import pytest

from app import config, camera_profiles
from app.recording import RecordingManager
from app.recording_settings import RecordingSettings
from app.site_profile import SiteProfile, load_site_profile, sync_recording_policy


ROOT = Path(__file__).resolve().parents[2]


def profile():
    return SiteProfile.model_validate({"schema_version": 1, "name": "test-river",
        "recording": {"enabled": True, "auto_horizontal_follow": True, "detail_capture_priority": True},
        "camera_optics": {"zoom_calibration": {"1": .03, "2": .06},
            "zoom_space_uri": "http://camera/ZoomSpace", "zoom_timeout_seconds": 45, "zoom_tolerance": .005}})


def test_shared_profile_seeds_once_and_never_overwrites_valid_local_edits(tmp_path):
    path = tmp_path / "settings.json"
    shared = profile()
    initial = sync_recording_policy(path, RecordingSettings(), shared)
    assert initial.auto_horizontal_follow and initial.detail_capture_priority
    local = initial.model_copy(update={"enable_ship_name": False, "pan_dead_zone": .25})
    path.write_text(local.model_dump_json(), encoding="utf-8")
    assert sync_recording_policy(path, local, shared) == local
    changed = shared.model_copy(update={"recording": shared.recording.model_copy(update={"pan_dead_zone": .2})})
    (tmp_path / "settings.shared-profile.json").unlink()
    saved_bytes = path.read_bytes()
    updated = sync_recording_policy(path, local, changed)
    assert updated == local and path.read_bytes() == saved_bytes
    assert not (tmp_path / "settings.before-shared-profile.json").exists()


def test_invalid_defaults_cannot_block_existing_working_local_settings(tmp_path):
    settings = RecordingSettings(enable_draft=False)
    (tmp_path / "settings.json").write_text(settings.model_dump_json(), encoding="utf-8")
    shared = tmp_path / "shared.json"
    shared.write_text('{"schema_version":2}', encoding="utf-8")
    manager = RecordingManager(tmp_path, site_profile_path=shared)
    try:
        assert manager.settings == settings
        assert manager.error is None
        with pytest.raises(ValueError, match="默认现场配置无法读取"):
            manager.get_default_settings()
        assert manager.settings == settings
    finally:
        manager.executor.shutdown()
        manager.control_executor.shutdown()


def test_valid_shared_profile_recovers_corrupt_local_settings_even_if_version_is_unchanged(tmp_path):
    shared = profile()
    path = tmp_path / "shared.json"
    path.write_text(shared.model_dump_json(), encoding="utf-8")
    settings = tmp_path / "settings.json"
    sync_recording_policy(settings, RecordingSettings(), shared)
    settings.write_text("broken-json", encoding="utf-8")
    manager = RecordingManager(tmp_path, site_profile_path=path)
    try:
        assert manager.settings == shared.recording
        assert manager.error is None
    finally:
        manager.executor.shutdown()
        manager.control_executor.shutdown()


def test_camera_profile_fills_optics_without_overwriting_explicit_local_calibration(tmp_path, monkeypatch):
    path = tmp_path / "shared.json"
    path.write_text(profile().model_dump_json(), encoding="utf-8")
    settings = replace(config.Settings(), site_profile_path=path,
        camera_onvif_zoom_calibration="", camera_onvif_zoom_space_uri="")
    monkeypatch.delenv("CAMERA_ONVIF_ZOOM_TIMEOUT", raising=False)
    monkeypatch.setattr(camera_profiles.camera_profile_service, "effective", lambda value: value)
    monkeypatch.setattr(config.model_config_service, "effective_settings", lambda: settings)
    effective = config.get_effective_settings()
    assert effective.camera_onvif_zoom_timeout == 45
    assert json.loads(effective.camera_onvif_zoom_calibration)["2.0"] == .06
    settings = replace(settings, camera_onvif_zoom_calibration='{"1":0,"4":0.4}')
    assert config.get_effective_settings().camera_onvif_zoom_calibration == settings.camera_onvif_zoom_calibration


def test_checked_in_shared_profile_is_valid_and_contains_no_private_host_fields():
    shared = load_site_profile(ROOT / "config/river-monitoring.json")
    assert shared.recording.detail_capture_priority and shared.recording.auto_horizontal_follow
    assert shared.recording.enable_ship_name is False and shared.recording.enable_draft is False
    serialized = shared.model_dump_json().lower()
    for private in ("password", "username", "rtsp://", "live_capture_url", "e:\\", "d:\\"):
        assert private not in serialized


def test_prepare_local_config_preserves_credentials_and_explicit_profile_opt_out(tmp_path):
    spec = importlib.util.spec_from_file_location("prepare_local_config", ROOT / "scripts/prepare-local-config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / ".env"
    path.write_text("CAMERA_ONVIF_PASSWORD='private-test'\nDEVICE=cpu\n", encoding="utf-8")
    module.prepare(tmp_path)
    assert "private-test" in path.read_text()
    assert "SITE_PROFILE" in path.read_text()
    path.write_text("CAMERA_ONVIF_PASSWORD='private-test'\nSITE_PROFILE=\n", encoding="utf-8")
    assert module.prepare(tmp_path)["shared_profile_enabled"] is False


def test_shared_camera_calibration_rejects_ambiguous_native_stops():
    document = profile().model_dump(mode="json")
    document["camera_optics"]["zoom_calibration"] = {"1": .03, "2": .03}
    with pytest.raises(ValueError): SiteProfile.model_validate(document)


def test_saved_policy_survives_restart_shared_update_and_explicit_default_reset(tmp_path):
    shared_path = tmp_path / "shared.json"
    first_profile = profile()
    shared_path.write_text(first_profile.model_dump_json(), encoding="utf-8")
    root = tmp_path / "passages"
    manager = RecordingManager(root, site_profile_path=shared_path)
    try:
        saved = manager.update_settings({"enabled": False, "line_patrol_enabled": True,
            "overview_line_left": .27, "overview_line_right": .71,
            "detail_line_left": .32, "detail_line_right": .78,
            "pan_max_left_travel_ratio": .08, "pan_max_right_travel_ratio": .22,
            "zoom_view_limits_enabled": False, "enable_ship_name": False, "enable_draft": False})
    finally:
        manager.executor.shutdown()
        manager.control_executor.shutdown()
    changed = first_profile.model_copy(update={"recording": first_profile.recording.model_copy(update={"pan_dead_zone": .2})})
    shared_path.write_text(changed.model_dump_json(), encoding="utf-8")
    (root / "settings.shared-profile.json").unlink()
    restarted = RecordingManager(root, site_profile_path=shared_path)
    try:
        assert restarted.get_settings() == saved and restarted.error is None
        before = restarted.settings_path.read_bytes()
        defaults = restarted.get_default_settings()
        assert defaults == changed.recording.model_dump()
        assert restarted.get_settings() == saved and restarted.settings_path.read_bytes() == before
        restarted.update_settings(defaults)
    finally:
        restarted.executor.shutdown()
        restarted.control_executor.shutdown()
    reset_restarted = RecordingManager(root, site_profile_path=shared_path)
    try:
        assert reset_restarted.get_settings() == defaults
    finally:
        reset_restarted.executor.shutdown()
        reset_restarted.control_executor.shutdown()


def test_existing_policy_is_retained_if_shared_file_was_removed(tmp_path):
    local = RecordingSettings(enabled=False, detail_line_right=.82)
    (tmp_path / "settings.json").write_text(local.model_dump_json(), encoding="utf-8")
    manager = RecordingManager(tmp_path, site_profile_path=tmp_path / "missing.json")
    try:
        assert manager.get_settings() == local.model_dump() and manager.error is None
    finally:
        manager.executor.shutdown()
        manager.control_executor.shutdown()
