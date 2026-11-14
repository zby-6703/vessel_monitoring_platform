"""Package only checked-in model-manifest files; never copy .env or data."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.deployment import package_models

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/deployment/river-models.zip")
    args = parser.parse_args()
    print(json.dumps(package_models(ROOT, args.output), ensure_ascii=False))
