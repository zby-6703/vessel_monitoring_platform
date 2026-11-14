"""Check this host offline. No camera, PTZ, database or network calls."""
from __future__ import annotations
import argparse
from importlib import import_module
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def check(*, verify_models=False, smoke_models=False, device="cpu"):
    from app.config import get_settings, model_config_service
    from app.deployment import check_model_files
    from app.site_profile import load_site_profile
    report = {"camera_contacted": False, "checks": []}
    def add(name, passed, detail):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
    add("python", sys.version_info[:2] == (3, 11), sys.version.split()[0])
    for module in ("fastapi", "uvicorn", "sqlalchemy", "pymysql", "redis", "cv2", "numpy", "scipy", "onnxruntime", "httpx", "yaml", "PIL", "pypinyin"):
        try:
            import_module(module)
            add(f"import:{module}", True, "ready")
        except Exception as exc:
            add(f"import:{module}", False, type(exc).__name__)
    add("ffmpeg", shutil.which("ffmpeg") is not None, "required for passage video encoding")
    add("private_config", (ROOT / ".env").is_file(), "local .env")
    add("frontend", (ROOT / "frontend/dist/index.html").is_file(), "npm run build")
    try:
        profile = load_site_profile(get_settings().site_profile_path or ROOT / "config/river-monitoring.json")
        add("shared_profile", True, f"{profile.name} / {profile.digest()[:12]}")
    except Exception as exc:
        add("shared_profile", False, type(exc).__name__)
    try:
        for item in check_model_files(ROOT, verify_hashes=verify_models):
            add(item["path"], item["status"] == "ready", item["status"])
    except Exception as exc:
        add("model_manifest", False, type(exc).__name__)
    if smoke_models and all(item["passed"] for item in report["checks"]):
        from dataclasses import replace
        from app.inference.model_registry import ModelRegistry
        import numpy as np
        models = ModelRegistry(replace(model_config_service.effective_settings(), device=device))
        try:
            models.load(realtime=False)
            output = models.ship_detector(np.zeros((544, 960, 3), dtype=np.uint8))
            add("onnx_smoke", "boxes" in output, f"all models loaded; detector executed on {device}")
        except Exception as exc:
            add("onnx_smoke", False, type(exc).__name__)
        finally:
            models.release()
    report["passed"] = all(item["passed"] for item in report["checks"])
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-models", action="store_true")
    parser.add_argument("--smoke-models", action="store_true")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = check(verify_models=args.verify_models, smoke_models=args.smoke_models, device=args.device)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            print(f"{'OK' if item['passed'] else 'FAIL'} {item['name']}: {item['detail']}")
        print("Camera was not contacted. " + ("Deployment checks passed." if report["passed"] else "Resolve failed checks before starting."))
    raise SystemExit(0 if report["passed"] else 1)
