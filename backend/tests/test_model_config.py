from pathlib import Path

from app.config import MODEL_FIELDS, ModelConfigService


def test_model_config_persists_validated_paths_and_resets(tmp_path: Path):
    config = tmp_path / "model.yml"
    config.write_text("Architecture:\n  name: TestModel\n", encoding="utf-8")
    weights = tmp_path / "model.onnx"
    weights.write_bytes(b"weights")
    values = {
        field: str(config if field.endswith("_config") else weights)
        for field in MODEL_FIELDS
    }
    service = ModelConfigService()
    service.path = tmp_path / "model_settings.json"
    saved = service.save(values)
    assert set(saved["values"]) == set(MODEL_FIELDS)
    assert all(source == "override" for source in saved["sources"].values())
    reset = service.reset()
    assert not service.path.exists()
    assert all(source == "environment" for source in reset["sources"].values())

