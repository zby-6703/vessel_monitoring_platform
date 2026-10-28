"""One physical zoom-in / zoom-out operation per saved camera recording.

All methods run under RecordingManager.lock. Lens I/O uses a separate worker;
capture keeps writing the actual camera frames while the lens is moving.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import time


class OpticalCycleMixin:
    def _initialize_optical_cycle(self):
        self.control_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="optical-zoom")
        self.zoom_controller = None
        self._control_future = None
        self._control_target = None
        self._control_started = 0.
        self._cycle_generation = 0
        self._optical_session = False
        self._cycle_halt = False
        self._cycle_no_more_frames = False
        self._cycle_policy = None
        self._cycle_exit_reason = "vessel_left"
        self._overview_since = None
        self._empty_since = None
        self._zoomed_since = None
        self._zoomed_ship_seen = False
        self.phase = "overview"
        self.current_zoom_factor = None
        self.cycle_count = 0

    def set_zoom_controller(self, controller):
        with self.lock:
            self.zoom_controller = controller

    def optical_control_busy(self) -> bool:
        with self.lock:
            return bool(self._control_future is not None or self._optical_session and self.settings.enabled)

    def zoom_observation_context(self) -> dict:
        with self.lock:
            return {"generation": self._cycle_generation, "phase": self.phase,
                    "accept_detections": self.phase in {"overview", "zoomed"} and not self._cycle_halt}

    def _phase(self, phase):
        self.phase = phase
        self._cycle_generation += 1
        self.hits = 0
        if phase == "rearming":
            self._overview_since = None
        if phase == "zoomed":
            self._empty_since = None
            self._zoomed_since = None
            self._zoomed_ship_seen = False

    def _zoom_event(self, target, settled):
        if self.active:
            self.catalog[self.active["id"]].setdefault("zoom_events", []).append({
                "source_seconds": self.last_timestamp, "factor": target, "settled": settled})

    def _command_zoom(self, target: float, phase: str):
        self._phase(phase)
        self._control_target = target
        self._control_started = time.monotonic()
        self._zoom_event(target, False)
        self.current_zoom_factor = None
        future = self.control_executor.submit(self.zoom_controller.move_to, target)
        self._control_future = future
        # A completed future invokes this callback immediately. Assign it first.
        session_id = self.session_id
        future.add_done_callback(lambda completed: self._zoom_completed(completed, session_id))

    def _zoom_completed(self, future, session_id):
        with self.lock:
            if future is not self._control_future:
                return
            target = self._control_target
            self._control_future = None
            if session_id != self.session_id:
                # A previous camera's return must never close a new local or
                # camera session's file. The serial worker still homes it.
                return
            try:
                result = future.result()
                if not isinstance(result, dict) or result.get("settled") is not True or not math.isclose(float(result.get("factor", -1)), target, abs_tol=.001):
                    raise ValueError("镜头未确认到达指定倍率")
            except Exception:
                self.current_zoom_factor = None
                if self._cycle_halt and target != 1:
                    self._command_zoom(1., "zooming_out")
                    return
                self.error = "光学变焦或到位确认失败，已停止循环；请检查摄像头与倍率标定"
                self._cycle_halt = True
                self._cycle_exit_reason = "zoom_failed"
                if target != 1:
                    # Attempt a physical return even after a failed zoom-in.
                    self._command_zoom(1., "zooming_out")
                else:
                    self._finish("zoom_failed")
                    self._phase("error")
                return
            self.current_zoom_factor = target
            self._zoom_event(target, True)
            if target == 1:
                if self.active:
                    self.catalog[self.active["id"]]["returned_to_overview"] = True
                if self._cycle_halt or self._cycle_no_more_frames:
                    self._finish(self._cycle_exit_reason)
                    self._phase("error" if self.error else "overview")
                    self._optical_session = False
                else:
                    self._phase("rearming")
            elif self._cycle_halt:
                self._command_zoom(1., "zooming_out")
            else:
                self._phase("zoomed")

    def _check_zoom_timeout(self):
        policy = self._cycle_policy or self.settings
        if self._control_future is None or time.monotonic() - self._control_started <= policy.zoom_settle_timeout_seconds:
            return
        target = self._control_target
        cancel = getattr(self.zoom_controller, "cancel_move", None)
        if cancel:
            cancel()
        # Invalidate callbacks from a timed-out operation. The serial motor
        # worker finishes it before processing the queued return command.
        self._control_future = None
        self.current_zoom_factor = None
        self.error = "镜头未在限定时间内到位，已停止本轮；请检查摄像头连接和变焦行程"
        self._cycle_halt = True
        self._cycle_exit_reason = "zoom_timeout"
        if target != 1 and self.zoom_controller is not None:
            self._command_zoom(1., "zooming_out")
        else:
            self._finish("zoom_timeout")
            self._phase("error")

    def _reset_optical_source(self):
        if self._optical_session or self._control_future is not None:
            self._cycle_halt = True
            self._finish("source_changed")
            if self.zoom_controller is not None:
                self._command_zoom(1., "initializing")
        self._optical_session = False
        self._cycle_policy = None
        self._cycle_no_more_frames = False
        self.cycle_count = 0
        self._cycle_generation += 1
        self._phase("overview")

    def _halt_optical(self, reason: str, *, no_more_frames: bool = True):
        self._cycle_halt = True
        self._cycle_no_more_frames = no_more_frames
        self._cycle_exit_reason = reason
        self._clear_buffer()
        self.hits = 0
        if self._control_future is not None and self._control_target != 1:
            cancel = getattr(self.zoom_controller, "cancel_move", None)
            if cancel:
                cancel()
        if self.zoom_controller is None:
            self._finish(reason)
            self._phase("error" if self.error else "overview")
            self._optical_session = False
        elif self._control_future is None:
            if self.current_zoom_factor == 1:
                self._finish(reason)
                self._phase("error" if self.error else "overview")
                self._optical_session = False
            else:
                self._command_zoom(1., "zooming_out")
        # An outstanding zoom-in callback queues the return itself. An
        # outstanding return callback closes the clip only after confirmation.

    def wait_for_optical_stop(self, timeout: float = 35) -> bool:
        """Bounded shutdown drain, including a return queued by a callback."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                future = self._control_future
            if future is None:
                return True
            try:
                future.result(timeout=max(.01, deadline - time.monotonic()))
            except TimeoutError:
                with self.lock:
                    self._check_zoom_timeout()
                return False
            except Exception:
                pass
            # result() can return before its done callback acquires the lock.
            time.sleep(.005)
        return False

    def _qualify_optical(self, vessels):
        policy = self._cycle_policy or self.settings
        return [v for v in vessels if float(v.get("confidence", 0)) >= policy.confidence_threshold
                and any(b.get("type") == "ship" for b in v.get("boxes", []))]

    def _record_optical_attributes(self, vessels):
        if not self.active:
            return
        item = self.catalog[self.active["id"]]
        for vessel in vessels:
            name, track_id = vessel.get("ship_name"), vessel.get("ship_id")
            if name and name != "UNKNOWN" and name not in item["ship_names"]:
                item["ship_names"].append(str(name))
            if isinstance(track_id, int) and track_id not in item["track_ids"]:
                item["track_ids"].append(track_id)

    def _process_optical_frame(self, image, fps, timestamp, vessels, observation_context):
        self.focus = {"x": 0., "y": 0., "width": 1., "height": 1.}
        self._check_zoom_timeout()
        if not self._optical_session:
            if not self.settings.enabled or self.error:
                return
            self._optical_session = True
            self._cycle_halt = self._cycle_no_more_frames = False
            self._cycle_policy = None
            if self.zoom_controller is None:
                self.error = "光学循环需要已标定的 ONVIF 绝对变焦摄像头，不能用数字放大代替"
                self._phase("error")
                return
            try:
                self.zoom_controller.validate_cycle(self.settings.zoom_factor)
            except (ValueError, RuntimeError):
                self.error = "摄像头光学倍率尚未标定或不受支持，请完成 1× 和目标倍率配置"
                self._phase("error")
                return
            self._command_zoom(1., "initializing")
            return
        policy = self._cycle_policy or self.settings
        if self.active:
            if image.shape[:2] != self.active["shape"]:
                self.error = "摄像头分辨率改变，已结束本轮并尝试缩回 1×"
                self._halt_optical("resolution_changed")
                return
            self._write_frame(image, timestamp)
        if self.phase == "rearming":
            if self._overview_since is None:
                self._overview_since = timestamp
            if timestamp - self._overview_since >= policy.overview_settle_seconds:
                if self.active:
                    self.cycle_count += 1
                    self._finish(self._cycle_exit_reason)
                self._cycle_policy = None
                self._phase("overview")
            # Require a subsequent inference captured after the panorama has
            # settled AND the previous recording was closed.
            return
        if self._cycle_halt or self.error or self.phase not in {"overview", "zoomed"}:
            return
        if self.active and timestamp - self.active["start"] >= policy.max_clip_seconds:
            self.error = "本轮录像超过安全时长，已停止循环并尝试缩回 1×；请检查船尾离场检测"
            self._halt_optical("cycle_timeout")
            return
        fresh = vessels is not None and (observation_context is None or
            observation_context.get("generation") == self._cycle_generation and observation_context.get("phase") == self.phase and observation_context.get("accept_detections") is True)
        if not fresh:
            return
        qualified = self._qualify_optical(vessels)
        if self.phase == "overview":
            self.hits = self.hits + 1 if qualified else 0
            if self.hits < policy.min_hits:
                return
            self._clear_buffer()
            self._cycle_policy = self.settings.model_copy(deep=True)
            self._cycle_exit_reason = "vessel_left"
            self._start(image, fps, timestamp, single=True)
            item = self.catalog[self.active["id"]]
            item.update(recording_mode="optical_cycle", cycle_index=self.cycle_count + 1,
                        optical_zoom_factor=policy.zoom_factor, returned_to_overview=False, zoom_events=[])
            self._record_optical_attributes(qualified)
            self._write_frame(image, timestamp)
            # Open the file and write the triggering full-view frame FIRST.
            self._command_zoom(policy.zoom_factor, "zooming_in")
        else:
            self._record_optical_attributes(qualified)
            if qualified:
                self._zoomed_ship_seen = True
                self._empty_since = None
            elif not self._zoomed_ship_seen:
                # At first detection in the wide view, the bow can still be
                # outside the narrower zoomed FOV. Wait for it to arrive before
                # an empty zoomed frame can mean that the stern has departed.
                self._empty_since = None
            elif self._empty_since is None:
                self._empty_since = timestamp
            elif timestamp - self._empty_since >= policy.departure_confirm_seconds:
                self._command_zoom(1., "zooming_out")
