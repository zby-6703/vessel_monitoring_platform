"""Capability-gated pan/tilt, focus position feedback, and monitor-view return."""
import json
import math
import time
from xml.sax.saxutils import escape, quoteattr

from .config import PLATFORM_ROOT, get_effective_settings


class CameraMotionMixin:
    def focus_options(self):
        settings = get_effective_settings()
        if not settings.camera_onvif_imaging_url or not settings.camera_onvif_source_token:
            raise ValueError("未配置 Imaging 服务和视频源")
        doc = self._request(settings, self.IMAGING, "GetMoveOptions", f"<t:VideoSourceToken>{escape(settings.camera_onvif_source_token)}</t:VideoSourceToken>")
        return self._range(doc.find(f".//{{{self.SCHEMA}}}Absolute/{{{self.SCHEMA}}}Position"))

    def _range(self, element):
        if element is None:
            raise ValueError("设备未声明所需的坐标范围")
        low = float(element.findtext(f"{{{self.SCHEMA}}}Min", "nan"))
        high = float(element.findtext(f"{{{self.SCHEMA}}}Max", "nan"))
        if not math.isfinite(low) or not math.isfinite(high) or high <= low:
            raise ValueError("设备返回无效坐标范围")
        return {"min": low, "max": high}

    def focus_position(self):
        settings = get_effective_settings()
        doc = self._request(settings, self.IMAGING, "GetStatus", f"<t:VideoSourceToken>{escape(settings.camera_onvif_source_token)}</t:VideoSourceToken>")
        status = doc.find(f".//{{{self.SCHEMA}}}FocusStatus20")
        if status is None:
            raise ValueError("设备没有提供焦点位置反馈")
        position = float(status.findtext(f"{{{self.SCHEMA}}}Position", "nan"))
        if not math.isfinite(position):
            raise ValueError("焦点位置反馈无效")
        movement = status.findtext(f"{{{self.SCHEMA}}}MoveStatus", "").upper()
        if movement != "IDLE":
            raise ValueError("焦点尚未停止运动")
        return position

    def move_focus(self, position):
        settings = get_effective_settings()
        source = f"<t:VideoSourceToken>{escape(settings.camera_onvif_source_token)}</t:VideoSourceToken>"
        self._request(settings, self.IMAGING, "Move", source +
            f"<t:Focus><tt:Absolute><tt:Position>{position:.12g}</tt:Position></tt:Absolute></t:Focus>")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                actual = self.focus_position()
                if abs(actual - position) <= .001:
                    return actual
            except ValueError as exc:
                if "尚未停止" not in str(exc):
                    raise
            time.sleep(.08)
        self._request(settings, self.IMAGING, "Stop", source)
        raise TimeoutError("焦点未在期限内到位")

    def motion_options(self):
        settings = get_effective_settings()
        token = settings.camera_onvif_ptz_configuration_token
        if not token:
            raise ValueError("请配置云台 Configuration Token")
        doc = self._request(settings, self.PTZ, "GetConfigurationOptions", f"<t:ConfigurationToken>{escape(token)}</t:ConfigurationToken>")
        result = {}
        for key, tag in (("continuous", "ContinuousPanTiltVelocitySpace"), ("absolute", "AbsolutePanTiltPositionSpace"), ("fov", "RelativePanTiltTranslationSpace")):
            spaces = doc.findall(f".//{{{self.SCHEMA}}}{tag}")
            if key == "fov":
                spaces = [s for s in spaces if s.findtext(f"{{{self.SCHEMA}}}URI", "").endswith("/TranslationSpaceFov")]
            if len(spaces) != 1:
                continue
            space = spaces[0]
            uri = space.findtext(f"{{{self.SCHEMA}}}URI", "")
            if uri:
                result[key] = {"space": uri, "x": self._range(space.find(f"{{{self.SCHEMA}}}XRange")), "y": self._range(space.find(f"{{{self.SCHEMA}}}YRange"))}
        result["timeout"] = doc.findtext(f".//{{{self.SCHEMA}}}PTZTimeout/{{{self.SCHEMA}}}Min", "PT1S")
        return result

    def pan_tilt(self, action):
        settings = get_effective_settings()
        options = self.motion_options()
        space = options.get("continuous")
        if not space:
            raise ValueError("设备未声明连续云台旋转能力")
        x, y = {"pan_left": (-1, 0), "pan_right": (1, 0), "tilt_up": (0, 1), "tilt_down": (0, -1)}[action]
        def velocity(direction, axis):
            bounds = space[axis]
            if not bounds["min"] <= 0 <= bounds["max"] or direction > 0 and bounds["max"] <= 0 or direction < 0 and bounds["min"] >= 0:
                raise ValueError("设备不支持该旋转方向")
            return bounds["max"] * .15 if direction > 0 else bounds["min"] * .15 if direction < 0 else 0
        profile = f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>"
        try:
            self._request(settings, self.PTZ, "ContinuousMove", profile +
                f'<t:Velocity><tt:PanTilt x="{velocity(x, "x"):.8g}" y="{velocity(y, "y"):.8g}" space={quoteattr(space["space"])}/></t:Velocity><t:Timeout>{escape(options["timeout"])}</t:Timeout>')
            time.sleep(.25)
        finally:
            self._request(settings, self.PTZ, "Stop", profile + "<t:PanTilt>true</t:PanTilt><t:Zoom>false</t:Zoom>")

    def _pan_position(self, settings):
        doc = self._request(settings, self.PTZ, "GetStatus", f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>")
        pan = doc.find(f".//{{{self.SCHEMA}}}Position/{{{self.SCHEMA}}}PanTilt")
        if pan is None or doc.findtext(f".//{{{self.SCHEMA}}}MoveStatus/{{{self.SCHEMA}}}PanTilt") != "IDLE":
            raise ValueError("无法确认云台停止及实际位置")
        x, y = float(pan.attrib["x"]), float(pan.attrib["y"])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("云台位置无效")
        return {"x": x, "y": y, "space": pan.attrib.get("space")}

    def monitor_view(self, restore=False):
        settings = get_effective_settings()
        path = PLATFORM_ROOT / "data" / "monitor_view.json"
        identity = [settings.camera_onvif_ptz_url, settings.camera_onvif_source_token]
        space = self.motion_options().get("absolute")
        if not space:
            raise ValueError("设备未声明绝对云台坐标，无法保存/恢复监测机位")
        if not restore:
            if self.read_magnification() != 1:
                raise ValueError("请先将镜头恢复到已标定的 1×，再保存监测机位")
            position = self._pan_position(settings)
            if position["space"] and position["space"] != space["space"]:
                raise ValueError("云台反馈与配置坐标空间不一致")
            from .recording import _atomic_json
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(path, {"identity": identity, "position": position, "space": space["space"]})
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("请先保存当前监测机位") from None
        if saved.get("identity") != identity or saved.get("space") != space["space"]:
            raise ValueError("保存的监测机位属于其他摄像头或坐标空间，请重新保存")
        position = saved["position"]
        for axis in ("x", "y"):
            if not space[axis]["min"] <= position[axis] <= space[axis]["max"]:
                raise ValueError("保存机位超出设备声明范围")
        self.move_to(1.)
        profile = f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>"
        self._request(settings, self.PTZ, "AbsoluteMove", profile +
            f'<t:Position><tt:PanTilt x="{position["x"]:.12g}" y="{position["y"]:.12g}" space={quoteattr(space["space"])}/></t:Position>')
        deadline = time.monotonic() + settings.camera_onvif_zoom_timeout
        try:
            while time.monotonic() < deadline:
                if self._cancel_move.is_set():
                    raise ValueError("监测机位恢复已取消")
                try:
                    actual = self._pan_position(settings)
                    if actual["space"] and actual["space"] != space["space"]:
                        raise ValueError("云台反馈与保存机位的坐标空间不一致")
                    if all(abs(actual[axis] - position[axis]) <= (space[axis]["max"] - space[axis]["min"]) * .002 for axis in ("x", "y")):
                        return
                except ValueError as exc:
                    if "无法确认" not in str(exc):
                        raise
                time.sleep(.15)
            raise TimeoutError("未确认云台回到监测机位")
        finally:
            self._request(settings, self.PTZ, "Stop", profile + "<t:PanTilt>true</t:PanTilt><t:Zoom>false</t:Zoom>")
