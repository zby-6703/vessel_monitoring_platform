"""One physical zoom-in / zoom-out operation per saved camera recording.

All methods run under RecordingManager.lock. Lens I/O uses a separate worker;
capture keeps writing the actual camera frames while the lens is moving.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections import deque
import math
import time
import httpx
import cv2
import numpy as np

from .vessel_selection import VesselTargetSelector
from .scene_motion import BackgroundMotionEstimator
from .monitoring_audit import record_event


class OpticalCycleMixin:
    TARGET_EVIDENCE_GRACE_SECONDS = 5.

    def _initialize_optical_cycle(self):
        self.control_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="optical-zoom")
        self.zoom_controller = None
        self._control_future = None
        self._control_target = None
        self._control_started = 0.
        self._zoom_retry_count = 0
        self._zoom_recovery_attempts = 0
        self._zoom_recovery_due = None
        self._zoom_failure_kind = None
        self._zoom_fallback_active = False
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
        self.target_selector = VesselTargetSelector()
        self.background_motion = BackgroundMotionEstimator()
        self.adaptive_selection = dict(self.target_selector.result)
        self.locked_target = None
        self._pending_target_id = None
        self._active_zoom_factor = None
        self._last_zoom_adjust = None
        self._locked_appearance = None
        self._initial_target_height = None
        self._media_pending = deque()
        self._media_pending_bytes = 0
        self._media_last_target = None
        self._media_target_box = None
        self._closed_cycle_id = None
        self._closed_cycle_pending = False

    @staticmethod
    def _source_target_box(vessel, width, height):
        """Image-plane coordinates only; this never assumes a physical zoom."""
        try:
            for box in vessel.get("boxes", []):
                if box.get("type") != "ship":
                    continue
                x, y, w, h = [float(value) for value in box["xywh"]]
                if all(math.isfinite(value) for value in (x, y, w, h)) and w > 0 and h > 0:
                    clipped = (min(1., max(0., x / width)), min(1., max(0., y / height)),
                               max(0., min(1., (x + w) / width)), max(0., min(1., (y + h) / height)))
                    if clipped[2] > clipped[0] and clipped[3] > clipped[1]:
                        return clipped
        except (ValueError, TypeError, KeyError, ZeroDivisionError):
            pass
        return None

    def _queue_optical_media(self, image, timestamp):
        if not self.active or self.active.get("media_stopped"):
            return
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise RuntimeError("无法缓存录像尾部帧")
        data = encoded.tobytes()
        self._media_pending.append((timestamp, data))
        self._media_pending_bytes += len(data)
        while self._media_pending and self._media_pending_bytes > self.BUFFER_BYTES:
            _, discarded = self._media_pending.popleft()
            self._media_pending_bytes -= len(discarded)

    def _flush_optical_media(self, through):
        while self._media_pending and self._media_pending[0][0] <= through:
            timestamp, data = self._media_pending.popleft()
            self._media_pending_bytes -= len(data)
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                self._write_frame(image, timestamp)

    def _clear_optical_media(self):
        self._media_pending.clear()
        self._media_pending_bytes = 0

    def _observe_media_target(self, vessel, image, timestamp):
        self._media_last_target = timestamp
        self._media_target_box = self._source_target_box(vessel, image.shape[1], image.shape[0])
        self._flush_optical_media(timestamp)

    def _positive_unconfirmed_target(self, image, vessels, observation_context):
        # Moving/unknown lens views cannot prove an empty scene. Fresh positive
        # tracker + image-plane evidence can still retain a visible target.
        if not self.locked_target or vessels is None:
            return None
        if observation_context is not None and (observation_context.get("generation") != self._cycle_generation
                or observation_context.get("phase") != self.phase):
            return None
        policy = self._cycle_policy or self.settings
        for vessel in vessels:
            if vessel.get("tracked") is False:
                continue
            if vessel.get("ship_id") != self.locked_target.get("ship_id"):
                continue
            if float(vessel.get("confidence", 0)) < policy.confidence_threshold:
                continue
            if (vessel.get("processing") or {}).get("reason") == "ambiguous_identity":
                continue
            box = self._source_target_box(vessel, image.shape[1], image.shape[0])
            previous = self._media_target_box
            if box is None or previous is None:
                continue
            old_h, new_h = previous[3] - previous[1], box[3] - box[1]
            old_w, new_w = previous[2] - previous[0], box[2] - box[0]
            old_c = ((previous[0] + previous[2]) / 2, (previous[1] + previous[3]) / 2)
            new_c = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            if not (.2 <= new_h / max(old_h, .001) <= 5 and .1 <= new_w / max(old_w, .001) <= 10
                    and math.dist(old_c, new_c) <= max(.2, old_w * .8, old_h * .8)):
                continue
            appearance = self.target_selector.appearance_for(image, vessel)
            similarity = self.target_selector.appearance_similarity(self._locked_appearance, appearance)
            if similarity is not None and similarity < .3:
                continue
            return vessel
        return None

    def _seal_optical_media(self, cutoff, reason):
        clip = self.active
        if not clip or clip.get("media_stopped"):
            return
        self._flush_optical_media(cutoff)
        self._clear_optical_media()
        clip["writer"].close()
        clip["media_stopped"] = True
        self.catalog[clip["id"]]["media_end_reason"] = reason

    def _end_unknown_media(self, policy):
        cutoff = self._media_last_target + policy.departure_confirm_seconds
        self._seal_optical_media(cutoff, "target_evidence_timeout")
        if self.active:
            self.catalog[self.active["id"]].update(zoom_fallback=True,
                target_last_seen_seconds=self._media_last_target)
            self._finalize_sealed_optical_media("target_evidence_timeout")

    def _finalize_sealed_optical_media(self, reason):
        if self.active and self.active.get("media_stopped"):
            self._closed_cycle_id = self.active["id"]
            self._closed_cycle_pending = True
            self._finish(reason)

    def _mark_closed_cycle_returned(self):
        if self._closed_cycle_id and self._closed_cycle_id in self.catalog:
            from .recording import _atomic_json
            item = self.catalog[self._closed_cycle_id]
            item["returned_to_overview"] = True
            _atomic_json(self.root / self._closed_cycle_id / "manifest.json", item)

    def set_zoom_controller(self, controller):
        with self.lock:
            self.zoom_controller = controller

    def optical_control_busy(self) -> bool:
        with self.lock:
            if self._can_accept_recovery_observation():
                return False
            return bool(self._control_future is not None or self._optical_session and self.settings.enabled)

    def _can_accept_recovery_observation(self):
        return (self.phase == "error" and self._zoom_failure_kind == "transport"
                and self._control_future is None and self.settings.enabled
                and not self._cycle_halt and not self._cycle_no_more_frames)

    def zoom_observation_context(self) -> dict:
        with self.lock:
            return {"generation": self._cycle_generation, "phase": self.phase,
                    "accept_detections": self.phase in {"overview", "zoomed"} and not self._cycle_halt,
                    "zoom_factor": self.current_zoom_factor}

    def observe_zoom_factor(self, factor, generation=None):
        """Accept a read-only lens measurement only for the same idle view."""
        with self.lock:
            if generation is not None and generation != self._cycle_generation:
                return
            if self._can_accept_recovery_observation():
                if factor == 1:
                    self.current_zoom_factor = 1.
                    self._zoom_event(1., True)
                    self._confirmed_zoom_recovery()
                return
            if self._optical_session or self._control_future is not None:
                return
            if factor is not None and (not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor < 1):
                return
            if self.current_zoom_factor != factor:
                self._cycle_generation += 1
                record_event("zoom_observation", generation=self._cycle_generation, zoom_factor=factor,
                    reason="zoom_unknown" if factor is None else "zoom_observed")
            self.current_zoom_factor = factor

    def invalidate_zoom_observation(self):
        with self.lock:
            self.current_zoom_factor = None
            self._cycle_generation += 1
            record_event("zoom_observation", generation=self._cycle_generation,
                zoom_factor=None, reason="zoom_unknown")

    def available_magnifications(self):
        getter = getattr(self.zoom_controller, "available_magnifications", None)
        return getter() if getter else [1., self.settings.zoom_factor]

    def _validate_zoom_policy(self, settings):
        if settings.adaptive_zoom_enabled:
            factors = [factor for factor in self.available_magnifications() if settings.min_optical_zoom <= factor <= settings.max_optical_zoom]
            if not factors:
                raise ValueError("自适应范围内没有已标定光学倍率，请先完成倍率标定或调整范围")
            self.zoom_controller.validate_cycle(min(factors))
        else:
            self.zoom_controller.validate_cycle(settings.zoom_factor)

    def validate_optical_settings(self):
        with self.lock:
            self._validate_zoom_policy(self.settings)

    def _phase(self, phase):
        self.phase = phase
        self._cycle_generation += 1
        record_event("optical_phase", phase=phase, generation=self._cycle_generation,
            zoom_factor=self.current_zoom_factor)
        self.hits = 0
        if phase == "rearming":
            self._overview_since = None
        if phase == "zoomed":
            self._empty_since = None
            self._zoomed_since = None
            self._zoomed_ship_seen = False

    def _zoom_event(self, target, settled):
        recording_id = self.active["id"] if self.active else self._closed_cycle_id
        if recording_id and recording_id in self.catalog:
            self.catalog[recording_id].setdefault("zoom_events", []).append({
                "source_seconds": self.last_timestamp, "factor": target, "settled": settled})

    def _command_zoom(self, target: float, phase: str, *, retry: bool = False):
        if not retry:
            self._zoom_retry_count = 0
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

    def _schedule_zoom_recovery(self, failed_target):
        """Keep the real frames while trying only a measured panorama return."""
        self._zoom_failure_kind = "transport"
        self._cycle_exit_reason = "zoom_transport_fallback"
        if self.active:
            self._zoom_fallback_active = True
            item = self.catalog[self.active["id"]]
            item.update(zoom_fallback=True, zoom_failure_kind="transport", last_failed_zoom_factor=failed_target)
            item.setdefault("failed_zoom_factor", failed_target)
            self._finalize_sealed_optical_media("zoom_transport_fallback")
        self._phase("recovering")
        if self._zoom_recovery_attempts >= 4:
            self._zoom_recovery_due = None
            self.error = "镜头网络请求持续失败，自动变焦已暂停；录像仅保留目标有证据出现的部分"
            self._phase("error")
            return
        delay = min(30., 5. * 2 ** self._zoom_recovery_attempts)
        self._zoom_recovery_due = time.monotonic() + delay
        self.error = "镜头网络请求失败，正在按目标可见证据保留原视频并恢复 1×，尚未确认镜头位置"
        record_event("optical_phase", phase="recovering", reason="zoom_transport_failed")

    def _recover_zoom_if_due(self):
        if (self.phase == "recovering" and not self._cycle_halt
                and self.settings.enabled and self._control_future is None
                and self._zoom_recovery_due is not None
                and time.monotonic() >= self._zoom_recovery_due):
            self._zoom_recovery_due = None
            self._zoom_recovery_attempts += 1
            self._command_zoom(1., "recovering")

    def _confirmed_zoom_recovery(self):
        self.error = None
        self._zoom_recovery_due = None
        record_event("optical_phase", phase="recovering", zoom_factor=1., reason="zoom_recovered")
        self._mark_closed_cycle_returned()
        if self.active:
            self.catalog[self.active["id"]]["returned_to_overview"] = True
            self._active_zoom_factor = 1.
            self._phase("rearming" if self.active.get("media_stopped") else "zoomed")
        else:
            self._phase("rearming")

    def _zoom_completed(self, future, session_id):
        with self.lock:
            if future is not self._control_future:
                return
            target = self._control_target
            command_phase = self.phase
            self._control_future = None
            if session_id != self.session_id:
                # A previous camera's return must never close a new local or
                # camera session's file. The serial worker still homes it.
                return
            try:
                result = future.result()
                if not isinstance(result, dict) or result.get("settled") is not True or not math.isclose(float(result.get("factor", -1)), target, abs_tol=.001):
                    raise ValueError("镜头未确认到达指定倍率")
            except Exception as exc:
                self.current_zoom_factor = None
                if (isinstance(exc, (httpx.TransportError, OSError))
                        and not self._cycle_halt and command_phase == "recovering"):
                    self._schedule_zoom_recovery(target)
                    return
                if (isinstance(exc, (httpx.TransportError, OSError))
                        and not self._cycle_halt and self._zoom_retry_count < 1):
                    # Absolute lens positioning is idempotent. Retry one
                    # interrupted request, never an unsupported command,
                    # authentication rejection or an unconfirmed timeout.
                    # Preserve the request's transport origin while the retry
                    # is outstanding. Its recorder deadline may expire before
                    # the worker returns another exception or acknowledgement.
                    self._zoom_failure_kind = "transport"
                    self._zoom_retry_count += 1
                    record_event("optical_phase", phase=command_phase,
                        zoom_factor=target, reason="zoom_retry")
                    self._command_zoom(target, command_phase, retry=True)
                    return
                if isinstance(exc, (httpx.TransportError, OSError)) and not self._cycle_halt:
                    self._schedule_zoom_recovery(target)
                    return
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
            if command_phase != "recovering" and not self._zoom_fallback_active:
                # A successful ordinary retry is no longer a network failure;
                # do not classify a later unrelated motor timeout as one.
                self._zoom_failure_kind = None
            if command_phase == "recovering" and target == 1 and not self._cycle_halt:
                self._confirmed_zoom_recovery()
            elif command_phase == "zooming_in" and not self._cycle_halt:
                # Adaptive scheduling can deliberately record at 1x when the
                # hull is already large or higher calibrated steps are unsafe.
                self._active_zoom_factor = target
                self._phase("zoomed")
            elif target == 1:
                self._mark_closed_cycle_returned()
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
        if self._zoom_failure_kind == "transport" and not self._cycle_halt:
            # A late/unknown reply is never a position acknowledgement. Keep
            # video and let the serial worker finish before another 1x attempt.
            self._schedule_zoom_recovery(target)
            return
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
        self.target_selector = VesselTargetSelector()
        self.background_motion = BackgroundMotionEstimator()
        self.adaptive_selection = dict(self.target_selector.result)
        self.locked_target = None
        self._pending_target_id = None
        self._active_zoom_factor = None
        self._locked_appearance = None
        self._initial_target_height = None
        self._clear_optical_media()
        self._media_last_target = None
        self._media_target_box = None
        self._closed_cycle_id = None
        self._closed_cycle_pending = False
        self._zoom_recovery_attempts = 0
        self._zoom_recovery_due = None
        self._zoom_failure_kind = None
        self._zoom_fallback_active = False
        self._cycle_generation += 1
        self._phase("overview")

    def _halt_optical(self, reason: str, *, no_more_frames: bool = True):
        self._cycle_halt = True
        self._zoom_recovery_due = None
        self._cycle_no_more_frames = no_more_frames
        self._cycle_exit_reason = reason
        if self.active:
            cutoff = self.last_timestamp if self.last_timestamp is not None else self.active["start"]
            if self._media_last_target is not None:
                policy = self._cycle_policy or self.settings
                cutoff = min(cutoff, self._media_last_target + policy.departure_confirm_seconds)
            self._seal_optical_media(cutoff, reason)
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

    def _consistent_locked_target(self, target):
        if not self.locked_target or target.get("identity_ambiguous"):
            return False
        previous, current = self.locked_target["box"], target["box"]
        old_y, new_y = (previous[1] + previous[3]) / 2, (current[1] + current[3]) / 2
        if abs(old_y - new_y) > max(.06, (previous[3] - previous[1]) * .6):
            return False
        if target["height_ratio"] < self._initial_target_height * .4:
            return False
        appearance = self.target_selector.tracks[target["target_id"]].appearance
        similarity = self.target_selector.appearance_similarity(self._locked_appearance, appearance)
        return similarity is None or similarity >= .3

    def _find_locked_target(self):
        targets = self.adaptive_selection["targets"]
        direct = next((target for target in targets if self.locked_target and target["target_id"] == self.locked_target["target_id"]), None)
        if direct and self._consistent_locked_target(direct):
            return direct
        # Rebind after tracker ID churn only with a distinctive appearance AND
        # consistent wide-view geometry. Ambiguous monochrome/occluded targets
        # are returned to overview instead of handing the lens to another ship.
        matches = []
        for target in targets:
            if target["motion_state"] == "stationary" or not self._consistent_locked_target(target):
                continue
            appearance = self.target_selector.tracks[target["target_id"]].appearance
            similarity = self.target_selector.appearance_similarity(self._locked_appearance, appearance)
            if similarity is not None and similarity >= .65:
                matches.append((similarity, target))
        matches.sort(key=lambda pair: pair[0], reverse=True)
        if matches and (len(matches) == 1 or matches[0][0] - matches[1][0] >= .15):
            return matches[0][1]
        if direct:
            self._cycle_exit_reason = "target_ambiguous"
        return None

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
                self._validate_zoom_policy(self.settings)
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
            if not self.active.get("media_stopped") and timestamp - self.active["start"] >= policy.max_clip_seconds:
                # Freeze media before a sparse/new source frame can pad beyond
                # the limit. Motor recovery has its own bounded lifetime.
                self._seal_optical_media(self.active["start"] + policy.max_clip_seconds, "cycle_timeout")
                self.error = self.error or "本轮录像超过安全时长，已停止循环并尝试缩回 1×"
                self._halt_optical("cycle_timeout")
                return
            if not self.active.get("media_stopped"):
                self._queue_optical_media(image, timestamp)
                if self.phase != "zoomed" or self._zoom_failure_kind == "transport":
                    observed = self._positive_unconfirmed_target(image, vessels, observation_context)
                    if observed:
                        self._observe_media_target(observed, image, timestamp)
                    elif (self._zoom_failure_kind == "transport" and self._media_last_target is not None
                            and timestamp - self._media_last_target >= self.TARGET_EVIDENCE_GRACE_SECONDS):
                        self._end_unknown_media(policy)
        self._recover_zoom_if_due()
        if self.phase == "rearming":
            if self._overview_since is None:
                self._overview_since = timestamp
            if timestamp - self._overview_since >= policy.overview_settle_seconds:
                if self.active:
                    self.cycle_count += 1
                    self._finish(self._cycle_exit_reason)
                elif self._closed_cycle_pending:
                    self.cycle_count += 1
                self._cycle_policy = None
                self.locked_target = None
                self._pending_target_id = None
                self._zoom_fallback_active = False
                self._zoom_failure_kind = None
                self._zoom_recovery_attempts = 0
                self._closed_cycle_id = None
                self._closed_cycle_pending = False
                self._clear_optical_media()
                self._phase("overview")
            # Require a subsequent inference captured after the panorama has
            # settled AND the previous recording was closed.
            return
        if self._cycle_halt or self.error or self.phase not in {"overview", "zoomed"}:
            return
        fresh = vessels is not None and (observation_context is None or
            observation_context.get("generation") == self._cycle_generation and observation_context.get("phase") == self.phase and observation_context.get("accept_detections") is True)
        if not fresh:
            return
        qualified = self._qualify_optical(vessels)
        actual_zoom = float(self.current_zoom_factor or 1.)
        background = self.background_motion.update(image, qualified, timestamp, generation=self._cycle_generation)
        self.adaptive_selection = self.target_selector.update(qualified, image.shape[1], image.shape[0], timestamp, policy,
            zoom_factor=actual_zoom, calibrated_factors=self.available_magnifications(), camera_motion=background, image=image)
        if self.phase == "overview":
            candidate = self.adaptive_selection["selected"]
            candidate_id = candidate["target_id"] if candidate else None
            self.hits = self.hits + 1 if candidate_id is not None and candidate_id == self._pending_target_id else 1 if candidate_id is not None else 0
            self._pending_target_id = candidate_id
            if self.hits < policy.min_hits:
                return
            self._clear_buffer()
            self._cycle_policy = self.settings.model_copy(deep=True)
            self._cycle_exit_reason = "vessel_left"
            self.locked_target = {key: value for key, value in candidate.items() if key not in {"boxes", "vessel", "assets"}}
            self._active_zoom_factor = self.adaptive_selection["zoom_factor"]
            self._locked_appearance = self.target_selector.tracks[candidate_id].appearance
            self._initial_target_height = candidate["height_ratio"]
            self._last_zoom_adjust = timestamp
            self._start(image, fps, timestamp, single=True)
            self._clear_optical_media()
            self._media_last_target = timestamp
            self._media_target_box = self._source_target_box(candidate, image.shape[1], image.shape[0])
            self._closed_cycle_id = None
            self._closed_cycle_pending = False
            item = self.catalog[self.active["id"]]
            item.update(recording_mode="optical_cycle", cycle_index=self.cycle_count + 1,
                        optical_zoom_factor=self._active_zoom_factor, initial_optical_zoom_factor=self._active_zoom_factor,
                        returned_to_overview=False, zoom_events=[], target_id=candidate_id,
                        target_height_ratio=candidate["height_ratio"], selection_reason=self.adaptive_selection["selection_reason"],
                        adaptive_zoom=policy.adaptive_zoom_enabled, target_decision=dict(self.locked_target))
            self._record_optical_attributes([candidate])
            self._write_frame(image, timestamp)
            # Open the file and write the triggering full-view frame FIRST.
            self._command_zoom(self._active_zoom_factor, "zooming_in")
        else:
            if self._zoomed_since is None:
                self._zoomed_since = timestamp
            target = self._find_locked_target()
            if target:
                self.locked_target = {**self.locked_target, **target}
                observed_vessel = self.target_selector.tracks[target["target_id"]].vessel
                self._observe_media_target(observed_vessel, image, timestamp)
                self._record_optical_attributes([observed_vessel])
                self._zoomed_ship_seen = True
                self._empty_since = None
                if policy.suppress_stationary and target["motion_state"] == "stationary":
                    self._cycle_exit_reason = "target_stationary"
                    self._seal_optical_media(timestamp, "target_stationary")
                    self._command_zoom(1., "zooming_out")
                    return
                if not self._zoom_fallback_active and policy.adaptive_zoom_enabled and timestamp - self._last_zoom_adjust >= policy.zoom_adjust_cooldown_seconds:
                    plan = self.target_selector.plan_zoom(target, policy, self.available_magnifications())
                    next_zoom, current = plan["zoom_factor"], actual_zoom
                    should_reduce = next_zoom < current and (target["height_ratio"] * current > policy.target_height_ratio * (1 + policy.zoom_adjust_hysteresis) or plan["max_safe_zoom"] < current * .98)
                    should_increase = next_zoom > current and plan["requested_zoom_factor"] >= next_zoom * (1 + policy.zoom_adjust_hysteresis)
                    if should_reduce or should_increase:
                        self._last_zoom_adjust = timestamp
                        self._active_zoom_factor = next_zoom
                        item = self.catalog[self.active["id"]]
                        item["optical_zoom_factor"] = next_zoom
                        item.setdefault("zoom_decisions", []).append({"source_seconds": timestamp, "height_ratio": target["height_ratio"], **plan})
                        self._command_zoom(next_zoom, "zooming_in")
                        return
            elif not self._zoomed_ship_seen:
                # At first detection in the wide view, the bow can still be
                # outside the narrower zoomed FOV. Wait for it to arrive before
                # an empty zoomed frame can mean that the stern has departed.
                self._empty_since = None
                if timestamp - self._zoomed_since >= policy.target_acquire_timeout_seconds:
                    self._cycle_exit_reason = "target_not_reacquired"
                    self._seal_optical_media(self._media_last_target + policy.departure_confirm_seconds,
                        "target_not_reacquired")
                    self._command_zoom(1., "zooming_out")
            elif self._empty_since is None:
                self._empty_since = timestamp
            elif timestamp - self._empty_since >= policy.departure_confirm_seconds:
                self._seal_optical_media(self._media_last_target + policy.departure_confirm_seconds, "vessel_left")
                self._command_zoom(1., "zooming_out")
