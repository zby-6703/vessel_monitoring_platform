import json
import zipfile

import pytest

from app.deployment import check_model_files, package_models, sha256_file


@pytest.fixture
def bundle_root(tmp_path):
    model = tmp_path / "models/test/model.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model-fixture")
    config = tmp_path / "config/model-files.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"schema_version": 1, "files": [{"path": "models/test/model.onnx", "bytes": model.stat().st_size,
                                           "sha256": sha256_file(model)}]}), encoding="utf-8")
    (tmp_path / ".env").write_text("PASSWORD=private", encoding="utf-8")
    return tmp_path


def test_model_bundle_contains_only_manifest_files_and_can_be_verified(bundle_root):
    output = bundle_root / "data/portable.zip"
    report = package_models(bundle_root, output)
    assert report["sha256"] == sha256_file(output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"models/test/model.onnx", "config/model-files.json"}


def test_corrupt_model_is_reported_and_not_packaged(bundle_root):
    (bundle_root / "models/test/model.onnx").write_bytes(b"wrong-bytes!!")
    assert check_model_files(bundle_root, verify_hashes=True)[0]["status"] == "mismatch"
    with pytest.raises(ValueError): package_models(bundle_root, bundle_root / "bundle.zip")


def test_packaging_never_replaces_an_existing_output(bundle_root):
    output = bundle_root / "bundle.zip"
    output.write_bytes(b"keep-existing-output")
    with pytest.raises(FileExistsError): package_models(bundle_root, output)
    assert output.read_bytes() == b"keep-existing-output"


def test_manifest_cannot_select_private_files_outside_models(bundle_root):
    manifest = bundle_root / "config/model-files.json"
    manifest.write_text(json.dumps({"schema_version": 1, "files": [{"path": ".env", "bytes": 1, "sha256": "x"}]}))
    with pytest.raises(ValueError): check_model_files(bundle_root)
