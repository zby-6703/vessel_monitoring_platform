"""Image admission, horizontal follow transitions and post-zoom hold policy."""
import math
import time
import httpx

from .detail_focus import detail_focus


class OpticalFollowingMixin:
    def _initialize_following(self):
        self._control_kind = "zoom"
        self._pan_offset = 0.
        self._pan_displaced = False
        self._follow_owned = False
        self._pan_limited = False
        self._last_pan_adjust = None
        self._body_since = None
        self._body_last_seen = None
        self._body_width = None
        self._body_hits = 0
        self._empty_hits = 0
        self._zoom_hold_since = None
        self._zoom_settled_pending = False

    def _move_cycle_zoom(self, target, phase, follow):
        result = self.zoom_controller.move_to(target)
        if follow:
            if phase == "initializing":
                self.zoom_controller.begin_horizontal_follow()
            if target == 1 and phase in {"initializing", "zooming_out", "recovering"}:
                home = self.zoom_controller.restore_horizontal_follow()
                if not home or home.get("settled") is not True:
                    raise RuntimeError("未确认回到水平监测机位")
                result = {**result, "pan_restored": True}
        return result

    def _body_ready(self, vessel, image, timestamp, policy):
        box = self._source_target_box(vessel, image.shape[1], image.shape[0])
        full = box and box[0] >= policy.body_entry_margin and box[2] <= 1 - policy.body_entry_margin
        width = box[2] - box[0] if box else 0.
        continuous = self._body_last_seen is not None and 0 < timestamp - self._body_last_seen <= 3
        stable = self._body_width is not None and abs(width - self._body_width) <= max(.01, self._body_width * .1)
        if not full or not continuous or not stable:
            self._body_since = timestamp if full else None
            self._body_hits = 1 if full else 0
            self._body_width = width if full else None
        else:
            self._body_hits += 1
        self._body_last_seen = timestamp
        return bool(full and self._body_since is not None and self._body_hits >= 3
                    and timestamp - self._body_since + 1e-6 >= policy.body_entry_confirm_seconds)

    def _centered_zoom_plan(self, target, vessel, image, policy):
        plan = self.target_selector.plan_zoom(target, policy, self.available_magnifications())
        box = self._source_target_box(vessel, image.shape[1], image.shape[0])
        if box:
            center = (box[0] + box[2]) / 2
            # Even without pan support, never zoom the known hull centre out
            # of the next view. Full hull width need not fit a detail shot.
            limit = .35 / max(abs(center - .5), .001)
            choices = [factor for factor in self.available_magnifications() if factor <= min(limit, plan["zoom_factor"]) + 1e-8]
            zoom = max(choices, default=1.)
            if zoom < plan["zoom_factor"]:
                plan.update(zoom_factor=zoom, selection_reason="horizontal_view_limited")
        return plan

    def _maybe_follow_horizontal(self, vessel, image, timestamp, policy, *, planned_zoom=None):
        if (not policy.auto_horizontal_follow or self._pan_limited or detail_focus.busy()
                or self._control_future is not None or self._cycle_halt or self._identity_fallback_active or self._zoom_fallback_active):
            return False
        if self._last_pan_adjust is not None and timestamp - self._last_pan_adjust < policy.pan_cooldown_seconds:
            return False
        box = self._source_target_box(vessel, image.shape[1], image.shape[0])
        if not box:
            return False
        center = (box[0] + box[2]) / 2
        dead_zone = min(policy.pan_dead_zone, .3 / planned_zoom) if planned_zoom and planned_zoom > 1 else policy.pan_dead_zone
        if abs(center - .5) <= dead_zone:
            return False
        step = max(-policy.pan_step_limit, min(policy.pan_step_limit, 2 * (center - .5)))
        self._last_pan_adjust = timestamp
        self._pan_displaced = True
        self._pan_resume_phase = self.phase
        self._control_kind = "pan"
        self._control_target = float(self.current_zoom_factor or 1.)
        self._control_started = time.monotonic()
        self._phase("panning", reset_acquisition=False)
        future = self.control_executor.submit(self.zoom_controller.follow_horizontal, step,
                                              max_travel_ratio=policy.pan_max_travel_ratio,
                                              zoom_factor=self._control_target)
        self._control_future = future
        session_id = self.session_id
        future.add_done_callback(lambda completed: self._pan_completed(completed, session_id))
        return True

    def _fail_horizontal_follow(self, error=None):
        self.current_zoom_factor = None
        self.error = "水平跟随或位置反馈失败，正在缩回 1× 并恢复水平监测机位"
        if error is not None:
            from .config import get_effective_settings
            from .live_capture import safe_camera_error
            detail = safe_camera_error(error, get_effective_settings())
            self.error += f"：{detail}"
            if self.active:
                self.catalog[self.active["id"]].setdefault("pan_errors", []).append({
                    "source_seconds": self.last_timestamp, "kind": type(error).__name__, "error": detail})
        if isinstance(error, (httpx.TransportError, OSError)) and not self._cycle_halt:
            # RelativeMove is not idempotent: never repeat an uncertain pan.
            # Recover with absolute panorama + original pan position instead.
            self._schedule_zoom_recovery(self._control_target)
            return
        self._cycle_halt = True
        self._cycle_exit_reason = "pan_failed"
        self._command_zoom(1., "zooming_out")

    def _pan_completed(self, future, session_id):
        with self.lock:
            if future is not self._control_future or session_id != self.session_id:
                return
            factor = self._control_target
            self._control_future = None
            try:
                result = future.result()
                step = float(result["fov_x"])
                if result.get("settled") is not True or not math.isfinite(step) or abs(step) > .6:
                    raise ValueError("水平移动未确认")
            except Exception as exc:
                self._fail_horizontal_follow(exc)
                return
            self._pan_offset += step / (2 * factor)
            self._pan_limited = bool(result.get("limited"))
            if self.active:
                self.catalog[self.active["id"]].setdefault("pan_events", []).append({
                    "source_seconds": self.last_timestamp, "zoom_factor": factor,
                    "fov_x": step, "pan_offset": self._pan_offset, "settled": True,
                    "limited": self._pan_limited, "before": result.get("before"), "after": result.get("after")})
            if self._cycle_halt:
                self._command_zoom(1., "zooming_out")
                return
            self._empty_since = self._association_lost_since = None
            self._empty_hits = 0
            self._body_since = self._body_last_seen = None
            self._body_hits = 0
            self._phase(self._pan_resume_phase, reset_acquisition=False)

    def _hold_zoom(self, timestamp, policy):
        return self.phase == "zoomed" and (self._zoom_hold_since is None
            or timestamp - self._zoom_hold_since < policy.zoom_min_hold_seconds)
