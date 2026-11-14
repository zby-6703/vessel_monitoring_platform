"""Offline deployment checks and portable, manifest-verified model bundles."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import zipfile


def sha256_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_files(root: Path):
    root = root.resolve()
    manifest = json.loads((root / "config/model-files.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list) or not manifest["files"]:
        raise ValueError("Invalid or empty model manifest")
    seen = set()
    for item in manifest["files"]:
        relative = Path(item["path"])
        resolved = (root / relative).resolve()
        if relative.is_absolute() or not resolved.is_relative_to(root / "models") or item["path"] in seen:
            raise ValueError("Invalid or duplicate model manifest path")
        seen.add(item["path"])
        yield item, resolved


def check_model_files(root: Path, *, verify_hashes=False):
    report = []
    for item, path in model_files(root):
        status = "ready" if path.is_file() else "missing"
        if status == "ready" and (path.stat().st_size != item["bytes"]
                or verify_hashes and sha256_file(path) != item["sha256"]):
            status = "mismatch"
        report.append({"path": item["path"], "status": status})
    return report


def package_models(root: Path, output: Path):
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root / "models"):
        raise ValueError("Bundle output must be outside the source model directory")
    if any(item["status"] != "ready" for item in check_model_files(root, verify_hashes=True)):
        raise ValueError("Model files are missing or differ from the checked-in manifest")
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            created = True
            for item, path in model_files(root):
                archive.write(path, item["path"])
            archive.write(root / "config/model-files.json", "config/model-files.json")
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    return {"path": str(output), "bytes": output.stat().st_size, "sha256": sha256_file(output)}
