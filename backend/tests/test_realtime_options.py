from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from app import api
from app.config import Settings
from app.inference.model_registry import ModelRegistry
from app.recording import RecordingManager
from test_target_processing import Models, frame, policy, vessel
from app.target_processing import RealtimeTargetProcessor
from app.inference.frame_inference import FrameInferenceService


@pytest.mark.parametrize("filter_enabled", [True, False])
@pytest.mark.parametrize("name,draft", [(False, False), (True, False), (False, True), (True, True)])
def test_each_realtime_option_controls_only_its_model_and_keeps_recording_admission(filter_enabled, name, draft):
    models = Models()
    processor = RealtimeTargetProcessor()
    settings = policy(target_filter_enabled=filter_enabled, enable_ship_name=name, enable_draft=draft,
                      target_confirm_hits=1, target_confirm_seconds=0)
    result = frame([vessel()])
    processor.process(FrameInferenceService(models), result, np.zeros((1000, 1000, 3), np.uint8), 0, settings)
    assert models.name_calls == int(name)
    assert models.draft_calls == int(draft)
    assert result.vessels[0].track_id == 1
    assert result.vessels[0].regions
    assert result.vessels[0].processing["eligible"]
    assert result.vessels[0].processing["detail_inferred"] == (name or draft)


def test_both_recognitions_disabled_still_trigger_real_passage_video(tmp_path):
    manager = RecordingManager(tmp_path)
    try:
        manager.update_settings({"enabled": True, "recording_mode": "passage", "enable_ship_name": False,
            "enable_draft": False, "pre_roll_seconds": 0, "target_filter_enabled": False})
        models = Models()
        image = np.full((1000, 1000, 3), 90, np.uint8)
        result = frame([vessel()])
        RealtimeTargetProcessor().process(FrameInferenceService(models), result, image, 0, manager.settings)
        live = api._live_frame(result, "test", {})
        manager.process_frame(image, camera_id="test", session_id="test", source_name="camera",
            fps=10, timestamp=0, vessels=live["vessels"])
        assert manager.active is not None
        assert manager.status()["media_recording"]
        assert models.name_calls == models.draft_calls == 0
    finally:
        manager.finish()
        manager.executor.shutdown(wait=True)
        manager.control_executor.shutdown(wait=True)


def test_realtime_detector_load_does_not_require_optional_packages(tmp_path, monkeypatch):
    from app.inference import onnx_backends
    config, weights = tmp_path / "det.yaml", tmp_path / "det.onnx"
    config.write_text("model_type: test")
    weights.write_bytes(b"test")
    settings = replace(Settings(), ship_detector_config=config, ship_detector_weights=weights,
        draftformer_config=None, draftformer_weights=None, shipname_config=None, shipname_weights=None)
    detector = object()
    monkeypatch.setattr(onnx_backends, "ONNXShipDETRPredictor", lambda **kwargs: detector)
    models = ModelRegistry(settings)
    models.load(realtime=True)
    assert models.ship_detector is detector
    assert models.name_recognizer is models.draft_predictor is None
    with pytest.raises(FileNotFoundError):
        models.ensure_details(enable_ship_name=True, enable_draft=False)
    assert models.ship_detector is detector
    models.release()


def test_toggle_change_releases_optional_models_and_drops_old_admission():
    models = Models()
    trims = []
    models.trim_details = lambda **kwargs: trims.append(kwargs)
    processor = RealtimeTargetProcessor()
    service = FrameInferenceService(models)
    image = np.zeros((1000, 1000, 3), np.uint8)
    for timestamp, enabled in [(0, True), (1, False), (2, True)]:
        result = frame([vessel()])
        processor.process(service, result, image, timestamp,
            policy(target_filter_enabled=False, enable_ship_name=enabled, enable_draft=False))
    assert models.name_calls == 2 and models.draft_calls == 0
    assert trims[1] == {"enable_ship_name": False, "enable_draft": False}
