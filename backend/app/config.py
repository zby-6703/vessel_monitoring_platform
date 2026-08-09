from __future__ import annotations

import os
import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

import yaml
from dotenv import load_dotenv


PLATFORM_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PLATFORM_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _path_env(name: str, default: str = "") -> Path | None:
    raw = os.getenv(name, default).strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (PLATFORM_ROOT / path).resolve()
    return path


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "8010"))
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/vessel_monitor.db")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_frame_queue: str = os.getenv("REDIS_FRAME_QUEUE", "video_stream_queue")
    redis_result_channel: str = os.getenv("REDIS_RESULT_CHANNEL", "vessel_inference_results")
    api_ingest_token: str = os.getenv("API_INGEST_TOKEN", "change-me")
    demo_mode: bool = _env_bool("DEMO_MODE", False)
    device: str = os.getenv("DEVICE", "cuda")
    result_root: Path = _path_env("RESULT_ROOT", "./data/results") or PLATFORM_ROOT / "data/results"
    upload_root: Path = _path_env("UPLOAD_ROOT", "./data/uploads") or PLATFORM_ROOT / "data/uploads"
    max_upload_bytes: int = int(float(os.getenv("MAX_UPLOAD_GB", "10")) * 1024**3)
    track_confirm_hits: int = int(os.getenv("TRACK_CONFIRM_HITS", "3"))
    track_lost_seconds: float = float(os.getenv("TRACK_LOST_SECONDS", "2"))
    track_finish_seconds: float = float(os.getenv("TRACK_FINISH_SECONDS", "8"))
    max_track_observations: int = int(os.getenv("MAX_TRACK_OBSERVATIONS", "300"))
    retention_days: int = int(os.getenv("RETENTION_DAYS", "30"))
    ais_live_retention_days: int = int(os.getenv("AIS_LIVE_RETENTION_DAYS", "7"))
    live_camera_id: str = os.getenv("LIVE_CAMERA_ID", "camera-01")
    live_camera_name: str = os.getenv("LIVE_CAMERA_NAME", "现场监测点 01")
    live_stream_url: str = os.getenv("LIVE_STREAM_URL", "").strip()
    live_stream_protocol: str = os.getenv("LIVE_STREAM_PROTOCOL", "HTTP-FLV")
    # Self-contained ONNX deployment packages.
    ship_detector_config: Path | None = _path_env(
        "SHIP_DETECTOR_CONFIG", "./models/shipdetr_DetShip-new/deploy_config.yaml"
    )
    ship_detector_weights: Path | None = _path_env(
        "SHIP_DETECTOR_WEIGHTS", "./models/shipdetr_DetShip-new/model.onnx"
    )
    draftformer_config: Path | None = _path_env(
        "DRAFTFORMER_CONFIG", "./models/draftformer_DetCharatersAndSegWater/deploy_config.yaml"
    )
    draftformer_weights: Path | None = _path_env(
        "DRAFTFORMER_WEIGHTS", "./models/draftformer_DetCharatersAndSegWater/model.onnx"
    )
    shipname_config: Path | None = _path_env(
        "SHIPNAME_CONFIG", "./models/svtrv2parseq_RecShipName/deploy_config.yaml"
    )
    shipname_weights: Path | None = _path_env(
        "SHIPNAME_WEIGHTS", "./models/svtrv2parseq_RecShipName/model.onnx"
    )
    demo_root: Path = PLATFORM_ROOT / "demo"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


MODEL_FIELDS = (
    "ship_detector_config", "ship_detector_weights", "draftformer_config",
    "draftformer_weights", "shipname_config", "shipname_weights",
)
CONFIG_FIELDS = {"ship_detector_config", "draftformer_config", "shipname_config"}


class ModelConfigService:
    """Persistent model-package overrides, kept with deployment settings."""

    def __init__(self) -> None:
        self.path = PLATFORM_ROOT / "data" / "model_settings.json"
        self._lock = RLock()

    def effective_settings(self) -> Settings:
        overrides = self.load()
        values = {field: Path(raw) for field, raw in overrides.items() if raw}
        return replace(get_settings(), **values) if values else get_settings()

    def load(self) -> dict[str, str]:
        with self._lock:
            if not self.path.is_file():
                return {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return {field: str(data[field]) for field in MODEL_FIELDS if data.get(field)}

    def current(self) -> dict[str, Any]:
        overrides = self.load()
        settings = self.effective_settings()
        return {
            "values": {field: str(getattr(settings, field) or "") for field in MODEL_FIELDS},
            "sources": {field: "override" if field in overrides else "environment" for field in MODEL_FIELDS},
            "override_file": str(self.path.resolve()),
        }

    def save(self, values: dict[str, str]) -> dict[str, Any]:
        normalized: dict[str, str] = {}
        errors: dict[str, str] = {}
        for field in MODEL_FIELDS:
            raw = str(values.get(field, "")).strip()
            if not raw:
                errors[field] = "路径不能为空"
                continue
            path = Path(raw)
            path = (PLATFORM_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
            if not path.is_file():
                errors[field] = f"文件不存在: {path}"
                continue
            if field in CONFIG_FIELDS:
                try:
                    content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    if "Architecture" not in content and "model_type" not in content:
                        errors[field] = "配置缺少 Architecture 或 model_type"
                        continue
                except Exception as exc:
                    errors[field] = f"无法解析 YAML: {exc}"
                    continue
            elif path.suffix.lower() != ".onnx":
                errors[field] = "部署权重仅支持 .onnx"
                continue
            normalized[field] = str(path)
        if errors:
            raise ValueError(json.dumps(errors, ensure_ascii=False))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        return self.current()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self.path.unlink(missing_ok=True)
        return self.current()


model_config_service = ModelConfigService()


def get_effective_settings() -> Settings:
    return model_config_service.effective_settings()
