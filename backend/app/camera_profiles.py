"""Read existing ONVIF media profiles and persist this platform's selection.

Never changes the camera's encoders or any other client's stream choice.
Private URIs are kept server-side; the public response contains metadata only.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .camera_setup import CameraSetupError, discover_camera
from .config import PLATFORM_ROOT, Settings
from .recording import _atomic_json


class CameraProfileService:
    def __init__(self, path: Path | None = None):
        self.path = path or PLATFORM_ROOT / "data" / "camera_profile.json"
        self.lock = threading.RLock()

    @staticmethod
    def _identity(settings):
        value = [settings.camera_onvif_ptz_url, settings.camera_onvif_username, settings.camera_onvif_password, settings.live_capture_url]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    def effective(self, settings: Settings) -> Settings:
        with self.lock:
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved["camera_identity"] != self._identity(settings):
                    return settings
                return replace(settings, **saved["values"])
            except (OSError, ValueError, KeyError, TypeError):
                return settings

    def discover(self, settings: Settings, token: str | None = None):
        endpoint = urlsplit(settings.camera_onvif_ptz_url)
        if not endpoint.hostname:
            raise CameraSetupError("请先配置摄像头 ONVIF 地址和凭据")
        return discover_camera(endpoint.hostname, settings.camera_onvif_username,
            settings.camera_onvif_password, port=endpoint.port or (443 if endpoint.scheme == "https" else 80),
            profile_token=token or settings.camera_onvif_profile_token or None,
            device_service_url=f"{endpoint.scheme}://{endpoint.netloc}/onvif/device_service", timeout=5.)

    def select(self, base: Settings, token: str):
        # Discovery gets the device's real URI and matching source/config tokens.
        discovered = self.discover(self.effective(base), token)
        env = discovered["env_values"]
        if base.camera_onvif_source_token and env["CAMERA_ONVIF_SOURCE_TOKEN"] != base.camera_onvif_source_token:
            raise CameraSetupError("请选择同一镜头的主流或辅助流；切换到另一视频源需要重新配置镜头标定")
        fields = ("LIVE_CAPTURE_URL", "CAMERA_ONVIF_PROFILE_TOKEN", "CAMERA_ONVIF_SOURCE_TOKEN",
                  "CAMERA_ONVIF_PTZ_CONFIGURATION_TOKEN", "CAMERA_ONVIF_PTZ_URL", "CAMERA_ONVIF_IMAGING_URL")
        values = {field.lower(): env[field] for field in fields}
        values["live_capture_fps_override"] = float(env.get("LIVE_CAPTURE_FPS_OVERRIDE", 0))
        # A pre-existing relay may point at a different profile. Preview the
        # selected server-side source through the existing frame WebSocket.
        values["live_stream_url"] = ""
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.path, {"camera_identity": self._identity(base), "values": values})
        return self.public(discovered, token)

    @staticmethod
    def public(discovered, selected):
        summary = discovered["summary"]
        return {"selected_profile": selected, "profiles": summary["profiles"],
                "device": summary["device"], "warnings": summary["warnings"]}


camera_profile_service = CameraProfileService()
