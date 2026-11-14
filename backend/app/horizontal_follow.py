"""Horizontal-only ONVIF FOV following, with feedback and a per-session origin.

FOV x=1 denotes the RIGHT image edge (half the image width), not a full
width. Relative commands always use y=0; returning preserves the measured
current tilt instead of restoring a stored vertical coordinate.
"""
import math
import json
import time
from xml.sax.saxutils import escape, quoteattr

from .config import PLATFORM_ROOT, get_effective_settings


class HorizontalFollowMixin:
    @staticmethod
    def _follow_identity(settings):
        return [settings.camera_onvif_ptz_url, settings.camera_onvif_source_token,
                settings.camera_onvif_ptz_configuration_token]

    def _save_follow_origin(self):
        from .recording import _atomic_json
        path = PLATFORM_ROOT / "data" / "horizontal_follow_origin.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, self._follow_origin)

    def validate_horizontal_follow(self):
        options = self.motion_options()
        if not options.get("absolute") or not options.get("fov"):
            raise ValueError("自动左右跟随需要设备的 FOV 相对云台空间和绝对位置反馈")
        fov = options["fov"]
        if not fov["x"]["min"] < 0 < fov["x"]["max"] or not fov["y"]["min"] <= 0 <= fov["y"]["max"]:
            raise ValueError("设备没有声明左右移动且垂直位移为零的能力")
        self._wait_for_idle_pan(get_effective_settings(), options)
        return options

    def _wait_for_idle_pan(self, settings, options):
        deadline = time.monotonic() + settings.camera_onvif_zoom_timeout
        while time.monotonic() < deadline:
            if self._cancel_move.is_set():
                raise RuntimeError("水平控制已取消")
            try:
                return self._checked_pan_position(settings, options)
            except ValueError as exc:
                if "无法确认" not in str(exc):
                    raise
            time.sleep(.1)
        raise TimeoutError("等待云台停止及实际位置反馈超时")

    def _checked_pan_position(self, settings, options):
        position = self._pan_position(settings)
        space = options["absolute"]
        if position["space"] and position["space"] != space["space"]:
            raise ValueError("云台反馈与配置坐标空间不一致")
        if not all(space[axis]["min"] <= position[axis] <= space[axis]["max"] for axis in ("x", "y")):
            raise ValueError("云台位置超出声明范围")
        return position

    def _wait_for_follow_zoom(self, settings, expected):
        deadline = time.monotonic() + settings.camera_onvif_zoom_timeout
        while time.monotonic() < deadline:
            if self._cancel_move.is_set():
                raise RuntimeError("水平跟随已取消")
            measured = self.read_magnification()
            if measured == expected:
                return
            if measured is not None:
                raise RuntimeError("其他控制端改变了水平跟随使用的倍率")
            time.sleep(.1)
        raise TimeoutError("等待水平跟随倍率及停止反馈超时")

    def begin_horizontal_follow(self):
        with self.lock:
            settings = get_effective_settings()
            options = self.validate_horizontal_follow()
            position = self._wait_for_idle_pan(settings, options)
            # Never replace an unrestored origin after a reconnect/retry.
            previous = getattr(self, "_follow_origin", None)
            if previous is None:
                path = PLATFORM_ROOT / "data" / "horizontal_follow_origin.json"
                if path.is_file():
                    previous = json.loads(path.read_text(encoding="utf-8"))
                    self._follow_origin = previous
            if previous and previous["identity"] == self._follow_identity(settings) and previous["displaced"]:
                previous["options"] = options
                self.restore_horizontal_follow()
                position = self._wait_for_idle_pan(settings, options)
            gain = None
            if (previous and previous.get("identity") == self._follow_identity(settings)
                    and previous.get("gain_profile") == settings.camera_onvif_profile_token
                    and abs(previous["position"]["y"] - position["y"]) <= (options["absolute"]["y"]["max"] - options["absolute"]["y"]["min"]) * .002):
                gain = previous.get("gain_at_one_x")
            self._follow_origin = {"identity": self._follow_identity(settings), "position": position,
                                   "last": position, "options": options, "displaced": False,
                                   "gain_profile": settings.camera_onvif_profile_token, "gain_at_one_x": gain}
            self._save_follow_origin()
            return dict(position)

    def follow_horizontal(self, fov_x, *, max_travel_ratio, zoom_factor):
        if not math.isfinite(fov_x) or abs(fov_x) > .6 or fov_x == 0:
            raise ValueError("无效的水平跟随步长")
        with self.lock:
            settings = get_effective_settings()
            origin = getattr(self, "_follow_origin", None)
            if not origin or origin["identity"] != self._follow_identity(settings):
                raise ValueError("水平跟随尚未保存本轮监测机位")
            if self._cancel_move.is_set():
                raise RuntimeError("水平跟随已取消")
            options = origin["options"]
            before = self._wait_for_idle_pan(settings, options)
            self._wait_for_follow_zoom(settings, zoom_factor)
            native = options["absolute"]
            tolerance = (native["x"]["max"] - native["x"]["min"]) * .0005
            movement_epsilon = (native["x"]["max"] - native["x"]["min"]) * .00001
            if abs(before["x"] - origin["last"]["x"]) > tolerance * 4:
                raise RuntimeError("其他控制端改变了云台位置，已暂停自动跟随")
            # Acknowledged tiny per-step feedback drift must not accumulate
            # into an alleged external tilt. Each step still checks its own
            # vertical feedback, and an intervening vertical jump is rejected.
            if abs(before["y"] - origin["last"]["y"]) > (native["y"]["max"] - native["y"]["min"]) * .002:
                raise RuntimeError("检测到外部上下移动，已暂停水平跟随")
            travel = (native["x"]["max"] - native["x"]["min"]) * max_travel_ratio
            used = before["x"] - origin["position"]["x"]
            if (fov_x > 0 and (used >= travel or before["x"] >= native["x"]["max"] - tolerance * 4)
                    or fov_x < 0 and (-used >= travel or before["x"] <= native["x"]["min"] + tolerance * 4)):
                return {"settled": True, "fov_x": 0., "limited": True, "before": before, "after": before}
            fov_x = max(options["fov"]["x"]["min"], min(options["fov"]["x"]["max"], fov_x))
            remaining = (min(native["x"]["max"], origin["position"]["x"] + travel) - before["x"] if fov_x > 0
                         else before["x"] - max(native["x"]["min"], origin["position"]["x"] - travel))
            gain = origin.get("gain_at_one_x")
            clamped = False
            if isinstance(gain, (int, float)) and math.isfinite(gain) and gain > 0:
                safe_step = max(0., remaining) * zoom_factor / gain * .8
                if safe_step < .03:
                    return {"settled": True, "fov_x": 0., "limited": True, "before": before, "after": before}
                if abs(fov_x) > safe_step:
                    fov_x = math.copysign(safe_step, fov_x)
                    clamped = True
            elif remaining < (native["x"]["max"] - native["x"]["min"]) * .05:
                # Without an observed FOV/native relation, do not risk the
                # first move wrapping a nearly exhausted native pan range.
                return {"settled": True, "fov_x": 0., "limited": True, "before": before, "after": before}
            profile = f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>"
            origin["displaced"] = True  # even a lost reply may have moved it
            self._save_follow_origin()
            deadline = time.monotonic() + settings.camera_onvif_zoom_timeout
            try:
                self._request(settings, self.PTZ, "RelativeMove", profile +
                    f'<t:Translation><tt:PanTilt x="{fov_x:.8g}" y="0" space={quoteattr(options["fov"]["space"])}/></t:Translation>')
                while time.monotonic() < deadline:
                    if self._cancel_move.is_set():
                        raise RuntimeError("水平跟随已取消")
                    try:
                        after = self._checked_pan_position(settings, options)
                    except ValueError as exc:
                        if "无法确认" not in str(exc):
                            raise
                    else:
                        delta = after["x"] - before["x"]
                        if abs(after["y"] - before["y"]) > (native["y"]["max"] - native["y"]["min"]) * .002:
                            raise RuntimeError("云台反馈出现垂直位移，已停止跟随")
                        if delta * fov_x < 0 and abs(delta) > movement_epsilon:
                            raise RuntimeError("云台反馈移动方向不符，已停止跟随")
                        if abs(delta) > movement_epsilon:
                            self._wait_for_follow_zoom(settings, zoom_factor)
                            origin["last"] = after
                            origin["gain_at_one_x"] = abs(delta / fov_x) * zoom_factor
                            self._save_follow_origin()
                            return {"settled": True, "fov_x": fov_x, "before": before, "after": after,
                                    "limited": clamped or abs(after["x"] - origin["position"]["x"]) >= travel}
                    time.sleep(.08)
                raise TimeoutError("水平云台未在限定时间内确认停止及实际移动")
            finally:
                self._request(settings, self.PTZ, "Stop", profile + "<t:PanTilt>true</t:PanTilt><t:Zoom>false</t:Zoom>")

    def restore_horizontal_follow(self):
        with self.lock:
            settings = get_effective_settings()
            origin = getattr(self, "_follow_origin", None)
            if not origin:
                return {"settled": True}
            if origin["identity"] != self._follow_identity(settings):
                raise ValueError("水平监测机位属于另一个摄像头配置")
            options = origin["options"]
            current = self._wait_for_idle_pan(settings, options)
            target_x = origin["position"]["x"]
            if not isinstance(target_x, (int, float)) or not math.isfinite(target_x) or not options["absolute"]["x"]["min"] <= target_x <= options["absolute"]["x"]["max"]:
                raise ValueError("保存的水平机位超出设备声明范围")
            tolerance = (options["absolute"]["x"]["max"] - options["absolute"]["x"]["min"]) * .0005
            if not origin["displaced"] and abs(current["x"] - target_x) <= tolerance:
                return {"settled": True}
            profile = f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>"
            deadline = time.monotonic() + settings.camera_onvif_zoom_timeout
            try:
                if abs(current["x"] - target_x) > tolerance:
                    self._request(settings, self.PTZ, "AbsoluteMove", profile +
                        f'<t:Position><tt:PanTilt x="{target_x:.12g}" y="{current["y"]:.12g}" space={quoteattr(options["absolute"]["space"])}/></t:Position>')
                while time.monotonic() < deadline:
                    if self._cancel_move.is_set():
                        raise RuntimeError("水平机位恢复已取消")
                    try:
                        actual = self._checked_pan_position(settings, options)
                    except ValueError as exc:
                        if "无法确认" not in str(exc):
                            raise
                    else:
                        if abs(actual["y"] - current["y"]) > (options["absolute"]["y"]["max"] - options["absolute"]["y"]["min"]) * .002:
                            raise RuntimeError("水平归位出现垂直位移，未确认归位成功")
                        if abs(actual["x"] - target_x) <= tolerance:
                            origin.update(last=actual, displaced=False)
                            self._save_follow_origin()
                            return {"settled": True}
                    time.sleep(.08)
                raise TimeoutError("未确认回到原水平监测机位")
            finally:
                self._request(settings, self.PTZ, "Stop", profile + "<t:PanTilt>true</t:PanTilt><t:Zoom>false</t:Zoom>")
