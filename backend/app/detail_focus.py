"""Bounded, image-feedback focus using real ONVIF focus positions.

The device need not expose a vendor-specific regional autofocus command. We
measure texture inside a selected ROI on fresh captured frames, search a small
native focus interval, then return to the best confirmed position. No OCR or
draft model is used. Camera I/O never runs on the capture/inference threads.
"""
from concurrent.futures import ThreadPoolExecutor
from statistics import median
import threading
import time

import cv2


def sharpness(image, region):
    height, width = image.shape[:2]
    left, top, right, bottom = region
    crop = image[max(0, int(top * height)):min(height, int(bottom * height)),
                 max(0, int(left * width)):min(width, int(right * width))]
    if min(crop.shape[:2], default=0) < 12:
        raise ValueError("聚焦区域过小，请选择船名、吃水刻度或有纹理的船体区域")
    if max(crop.shape[:2]) > 640:
        crop = cv2.resize(crop, None, fx=640 / max(crop.shape[:2]), fy=640 / max(crop.shape[:2]))
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def search_focus(control, sample, cancelled, region):
    """Compare at most five nearby positions; never scan the entire lens."""
    options = control.focus_options()
    initial = control.focus_position()
    low, high = options["min"], options["max"]
    if not low <= initial <= high:
        raise ValueError("焦点反馈超出设备声明范围")
    deadline = time.monotonic() + 15
    def score():
        if cancelled() or time.monotonic() > deadline:
            raise InterruptedError("区域对焦已停止或超时")
        return median(sharpness(sample(time.monotonic() + .12), region) for _ in range(2))
    best, baseline = initial, score()
    best_score = baseline
    if baseline < .5:
        raise ValueError("区域纹理不足，保留原焦点")
    try:
        for step in (.02, .005):
            center = best
            for target in (max(low, center - (high - low) * step), min(high, center + (high - low) * step)):
                if cancelled() or time.monotonic() > deadline:
                    raise InterruptedError("区域对焦已停止或超时")
                control.move_focus(target)
                value = score()
                # Small score changes from video compression or moving water
                # must not displace an already acceptable focus position.
                if value > best_score * 1.03:
                    best, best_score = target, value
        if cancelled():
            raise InterruptedError("区域对焦已停止")
        control.move_focus(best)
    except Exception:
        if not cancelled():
            control.move_focus(initial)
        raise
    return {"focus_position": best, "initial_position": initial,
            "sharpness_before": baseline, "sharpness_best": best_score}


class DetailFocus:
    def __init__(self):
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detail-focus")
        self.cancelled = threading.Event()
        self.latest = None
        self.future = None
        self.control = None
        self.state = {"status": "idle", "error": None, "region": None, "result": None}
        self.last_auto = 0.
        self.auto_key = None

    def observe(self, image, context):
        with self.lock:
            self.latest = (time.monotonic(), image, dict(context or {}))

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def busy(self):
        return bool(self.future and not self.future.done())

    def stop(self):
        self.cancelled.set()

    def wait(self, timeout=6):
        future = self.future
        if future:
            try:
                future.result(timeout=timeout)
            except TimeoutError:
                return False
            except Exception:
                # A completed worker must never prevent capture teardown.
                return True
        return True

    def start(self, control, region, *, automatic=False, completed=None):
        with self.lock:
            if self.busy():
                raise ValueError("区域对焦正在运行")
            if self.latest is None or time.monotonic() - self.latest[0] > 2:
                raise ValueError("请先启动摄像头采集，等待新画面")
            _, image, context = self.latest
            if context.get("accept_detections") is False:
                raise ValueError("请等待镜头停止运动后再对焦")
            sharpness(image, region)
            generation = context.get("generation")
            self.cancelled = threading.Event()
            cancel = self.cancelled
            self.state = {"status": "focusing", "error": None, "region": list(region), "result": None, "automatic": automatic}
            def interrupted():
                with self.lock:
                    current = self.latest[2] if self.latest else {}
                return cancel.is_set() or current.get("generation") != generation
            def sample(after):
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if interrupted():
                        raise InterruptedError("镜头视野已变化或聚焦已取消")
                    with self.lock:
                        latest = self.latest
                    if latest and latest[0] >= after:
                        return latest[1]
                    cancel.wait(.02)
                raise TimeoutError("未收到新的摄像头画面，聚焦已停止")
            def run():
                try:
                    with control.lock:
                        result = search_focus(control, sample, interrupted, region)
                    with self.lock:
                        self.state.update(status="completed", result=result)
                except InterruptedError:
                    with self.lock:
                        self.state.update(status="cancelled")
                except Exception as exc:
                    from .live_capture import safe_camera_error
                    from .config import get_effective_settings
                    with self.lock:
                        self.state.update(status="failed", error=safe_camera_error(exc, get_effective_settings()))
                finally:
                    if completed:
                        try:
                            completed(self.snapshot())
                        except OSError:
                            with self.lock:
                                self.state["error"] = "对焦结果保存失败，请检查录像存储"
            self.future = self.executor.submit(run)
            return self.snapshot()

    def consider(self, control, live, context, manager):
        policy = manager.get_settings()
        if not policy.get("auto_region_focus"):
            if self.snapshot().get("automatic"):
                self.stop()
            return
        status = manager.status()
        locked = status.get("locked_target") or {}
        if status.get("phase") != "zoomed" or not locked:
            return
        current_context = manager.zoom_observation_context()
        if not context or context.get("accept_detections") is not True or context.get("generation") != current_context.get("generation"):
            return
        generation = (context or {}).get("generation")
        key = (status.get("active_id"), generation)
        if not key[0] or key == self.auto_key or self.busy() or time.monotonic() - self.last_auto < 8:
            return
        candidates = [v for v in live["vessels"] if v.get("ship_id") == locked.get("ship_id") and v.get("tracked") is not False]
        if len(candidates) != 1 or candidates[0].get("processing", {}).get("reason") == "ambiguous_identity":
            return
        boxes = candidates[0]["boxes"]
        region = next((b for kind in ("ship_name_area", "waterline_area", "ship") for b in boxes if b["type"] == kind), None)
        if region is None:
            return
        width, height = live["source_width"], live["source_height"]
        x, y, w, h = region["xywh"]
        roi = (max(0, x / width), max(0, y / height), min(1, (x + w) / width), min(1, (y + h) / height))
        self.auto_key, self.last_auto = key, time.monotonic()
        try:
            self.start(control, roi, automatic=True, completed=lambda state: manager.record_focus_event(key[0], state))
        except ValueError:
            pass


detail_focus = DetailFocus()
