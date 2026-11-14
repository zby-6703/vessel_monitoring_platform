"""Initialize private host config without overwriting existing credentials."""
from __future__ import annotations
import argparse
from pathlib import Path
import secrets
import shutil

from dotenv import dotenv_values, set_key


def prepare(root: Path, runtime: str = "cpu"):
    path = root / ".env"
    created = not path.exists()
    if created:
        shutil.copyfile(root / ".env.example", path)
        set_key(str(path), "API_INGEST_TOKEN", secrets.token_urlsafe(32))
        set_key(str(path), "DEVICE", "cuda" if runtime == "gpu" else "cpu")
    values = dotenv_values(path)
    if "SITE_PROFILE" not in values:
        set_key(str(path), "SITE_PROFILE", "./config/river-monitoring.json")
    return {"created": created, "shared_profile_enabled": bool(dotenv_values(path).get("SITE_PROFILE"))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("cpu", "gpu"), default="cpu")
    args = parser.parse_args()
    print(prepare(Path(__file__).resolve().parents[1], args.runtime))
