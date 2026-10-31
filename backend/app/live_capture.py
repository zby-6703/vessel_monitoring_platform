"""Server-side camera capture, inference and optional ONVIF lens control.

Capture and inference have separate workers: an occupied model never prevents
the next source frame from reaching the event recorder. Inference uses a
one-frame queue so an overloaded camera cannot accumulate stale frames.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import queue
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable
from xml.sax.saxutils import escape

import cv2
import httpx

from .config import Settings, get_effective_settings
from .database import ResultRepository, SessionLocal
from .inference.frame_inference import FrameInferenceService
from .inference.model_registry import ModelRegistry
from .pipeline import Pipeline
from .monitoring_audit import record_event
from .recording import RecordingSettings, recording_manager
from .target_processing import RealtimeTargetProcessor
from .schemas import TaskKind
from .storage import ResultStorage
from .tracking import InstanceAggregator, TrackTrackTracker


def safe_camera_error(error: Exception | str, settings: Settings | None = None) -> str:
    """Errors may include authentication URLs; do not publish them to clients."""
    message = str(error)
    if settings:
        for secret in (settings.live_capture_url, settings.camera_onvif_password):
            if secret:
                message = message.replace(secret, "[redacted]")
    return re.sub(r"\b(?:rtsps?|https?)://[^\s<>]+", "[camera endpoint]", message)[:1200]


def _replace_latest(target: queue.Queue, value) -> None:
    try:
        target.put_nowait(value)
    except queue.Full:
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(value)
        except queue.Full:
            pass


def ensure_detection_available(result) -> None:
    critical = [str(error) for error in result.errors
                if str(error).startswith(("ship_detector_failed:", "frame_inference_failed:"))]
    if critical:
        raise RuntimeError("船舶检测失败，自动录制已停止：" + "; ".join(critical))


def validate_recording_interval(frame_step: int, fps: float, *, inference_seconds: float | None = None) -> None:
    """Do not accept a sampling rate that silently prevents confirmation."""
    config = recording_manager.get_settings()
    if not config["enabled"]:
        return
    if config.get("recording_mode") == "optical_cycle":
        # Optical departure needs consecutive NEW empty observations; an
        # inference gap is not an empty scene. Legacy post-roll timing is not
        # the departure-confirmation policy of this mode.
        return
    delay = config["post_roll_seconds"]
    interval = frame_step / fps
    if interval >= delay / 2:
        raise RuntimeError(f"推理采样间隔 {interval:.2f} 秒过大，无法可靠确认过船。请减少推理跳帧，或将离场延时设置为大于 {interval * 2:.2f} 秒后重新启动")
    if inference_seconds is not None and inference_seconds >= delay:
        raise RuntimeError(f"摄像头推理结果间隔/耗时达到 {inference_seconds:.2f} 秒，已超过离场延时 {delay:.2f} 秒。请提高离场延时或优化模型运行速度后重新启动")


class CameraCapture:
    ZOOM_OBSERVATION_INTERVAL = 5.
    ZOOM_OBSERVATION_JOIN_TIMEOUT = .5
    RECONNECT_INITIAL_SECONDS = 5.
    RECONNECT_MAX_SECONDS = 30.

    def __init__(self, broadcast: Callable, live_frame: Callable) -> None:
        self.broadcast = broadcast
        self.live_frame = live_frame
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        # Attempt teardown stops frame workers; only this separate token means
        # the operator/service cancelled the requested camera capture.
        self._shutdown_requested = threading.Event()
        self.thread: threading.Thread | None = None
        self.inference_thread: threading.Thread | None = None
        self.zoom_observation_thread: threading.Thread | None = None
        # Give each observer its own token. A network request completing after
        # stop must never publish into a newly started camera session.
        self._zoom_observation_stop = threading.Event()
        self._inference_error: str | None = None
        self._status = {"status": "idle", "session_id": None, "camera_id": None,
                        "filename": None, "frame_id": 0, "error": None,
                        "reconnect_attempt": 0, "retry_delay_seconds": None}

    def snapshot(self) -> dict:
        with self.lock:
            return {**self._status, "configured": bool(get_effective_settings().live_capture_url)}

    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive()) or bool(self.inference_thread and self.inference_thread.is_alive())

    def start(self, loop: asyncio.AbstractEventLoop, frame_step: int | None = None) -> dict:
        settings = get_effective_settings()
        if not settings.live_capture_url:
            raise ValueError("请先在服务端 .env 配置 LIVE_CAPTURE_URL，然后重新启动 API")
        with self.lock:
            if self.is_alive():
                raise RuntimeError("摄像头采集仍在运行或停止中")
            recording_manager.set_zoom_controller(onvif_control)
            config = recording_manager.get_settings()
            if config.get("enabled") and config.get("recording_mode") == "optical_cycle":
                recording_manager.validate_optical_settings()
            self.stop_event.clear()
            self._shutdown_requested.clear()
            self._zoom_observation_stop = threading.Event()
            self._inference_error = None
            self._status.update(status="starting", session_id=uuid.uuid4().hex,
                                camera_id=settings.live_camera_id, filename=settings.live_camera_name,
                                frame_id=0, error=None, reconnect_attempt=0, retry_delay_seconds=None)
            record_event("camera_start_requested", camera_id=self._status["camera_id"],
                session_id=self._status["session_id"], source_type="camera", frame_count=0)
            self.thread = threading.Thread(target=self._run, args=(settings, loop, frame_step or settings.live_capture_frame_step),
                                           name="camera-capture", daemon=True)
            self.thread.start()
            return self.snapshot()

    def stop(self) -> dict:
        self._shutdown_requested.set()
        self.stop_event.set()
        self._zoom_observation_stop.set()
        with self.lock:
            if self._status["status"] in {"starting", "running", "reconnecting"}:
                self._status["status"] = "stopped"
            return self.snapshot()

    def join(self, timeout: float = 12) -> None:
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout)

    def _publish(self, message: dict, loop: asyncio.AbstractEventLoop) -> None:
        try:
            pending = asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)
            pending.result(timeout=5)
        except Exception:
            # WebSocket delivery is optional; capture/recording must keep running
            # if there are no viewers or a browser connection has gone away.
            if 'pending' in locals():
                pending.cancel()

    def _run(self, settings: Settings, loop: asyncio.AbstractEventLoop, frame_step: int) -> None:
        models = None
        try:
            models = ModelRegistry(settings)
            models.load()
            attempt = 0
            while not self._shutdown_requested.is_set():
                # Never reuse model tensors or change source identity while an
                # older inference call still owns them.
                self.stop_event.clear()
                if self._shutdown_requested.is_set():
                    break
                self._zoom_observation_stop = threading.Event()
                self._inference_error = None
                if attempt:
                    with self.lock:
                        self._status.update(status="starting", session_id=uuid.uuid4().hex,
                            frame_id=0, error=None, retry_delay_seconds=None)
                    record_event("camera_start_requested", camera_id=self._status["camera_id"],
                        session_id=self._status["session_id"], source_type="camera", frame_count=0)
                failure, reason = self._run_attempt(settings, loop, frame_step, models)
                if self._shutdown_requested.is_set() or reason not in {"camera_connection_failed", "camera_read_failed"}:
                    break
                if self.inference_thread and self.inference_thread.is_alive():
                    with self.lock:
                        self._status.update(status="failed", error="上次推理尚未停止，自动重连已暂停；请等待后重新启动采集")
                    break
                drain = getattr(recording_manager, "wait_for_optical_stop", None)
                if drain is not None and not drain(timeout=settings.camera_onvif_zoom_timeout * 2 + 5):
                    with self.lock:
                        self._status.update(status="failed", error="镜头归位尚未完成，自动重连已暂停；请检查镜头后重新启动采集")
                    break
                attempt += 1
                delay = min(self.RECONNECT_MAX_SECONDS, self.RECONNECT_INITIAL_SECONDS * 2 ** min(attempt - 1, 3))
                with self.lock:
                    self._status.update(status="reconnecting", error=failure,
                        reconnect_attempt=attempt, retry_delay_seconds=delay)
                self._publish({"type": "camera_status", "data": self.snapshot()}, loop)
                if self._shutdown_requested.wait(delay):
                    break
        except Exception as exc:
            with self.lock:
                self._status.update(status="failed", error=safe_camera_error(exc, settings))
            record_event("camera_failed", camera_id=self._status["camera_id"],
                session_id=self._status["session_id"], source_type="camera", reason="model_failed")
        finally:
            self.stop_event.set()
            self._zoom_observation_stop.set()
            if self._shutdown_requested.is_set():
                with self.lock:
                    self._status.update(status="stopped", retry_delay_seconds=None)
            if models is not None and not (self.inference_thread and self.inference_thread.is_alive()):
                models.release()
            self._publish({"type": "camera_status", "data": self.snapshot()}, loop)

    def _run_attempt(self, settings: Settings, loop: asyncio.AbstractEventLoop, frame_step: int, models) -> tuple[str | None, str]:
        capture = None
        frames: queue.Queue = queue.Queue(maxsize=1)
        detections: queue.Queue = queue.Queue(maxsize=1)
        failure = None
        failure_reason = "capture_failed"
        try:
            if self.stop_event.is_set():
                return None, "source_stopped"
            service = FrameInferenceService(models)
            failure_reason = "camera_connection_failed"
            capture = cv2.VideoCapture()
            opened = capture.open(settings.live_capture_url, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
            if not opened:
                raise RuntimeError("无法连接摄像头视频源，请检查地址、账号、网络和编码格式")
            failure_reason = "capture_failed"
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            if settings.live_capture_fps_override:
                if not math.isfinite(settings.live_capture_fps_override) or not 1 <= settings.live_capture_fps_override <= 120:
                    raise ValueError("LIVE_CAPTURE_FPS_OVERRIDE 必须为 0（自动）或 1–120 之间的真实帧率")
                fps = settings.live_capture_fps_override
            elif not math.isfinite(fps) or fps < 1 or fps > 120:
                fps = settings.live_capture_fps
            validate_recording_interval(frame_step, fps)
            # Old idle lens knowledge is not evidence about this new source.
            # Native position queries happen off the capture thread: slow
            # ONVIF must not delay preview, detection or recorder frames.
            context = recording_manager.zoom_observation_context()
            if not recording_manager.optical_control_busy():
                recording_manager.observe_zoom_factor(None, context.get("generation"))
            with self.lock:
                self._status["status"] = "running"
                session = dict(self._status)
                session.update(fps=fps, frame_step=frame_step)
            record_event("camera_running", camera_id=session["camera_id"], session_id=session["session_id"],
                source_type="camera", frame_count=0)
            failure_reason = "capture_failed"
            observation_stop = self._zoom_observation_stop
            self.zoom_observation_thread = threading.Thread(target=self._observe_zoom,
                args=(settings, observation_stop), name="camera-zoom-observer", daemon=True)
            self.zoom_observation_thread.start()
            self.inference_thread = threading.Thread(target=self._inference_worker,
                args=(service, settings, session, frames, detections, loop), name="camera-inference", daemon=True)
            self.inference_thread.start()
            self._publish({"type": "camera_status", "data": self.snapshot()}, loop)
            frame_index = 0
            while not self.stop_event.is_set():
                # Tag before read and before processing prior results can issue
                # a lens command. A frame captured across a transition remains
                # stale rather than being relabelled as the new field of view.
                captured_context = recording_manager.zoom_observation_context()
                ok, image = capture.read()
                if not ok:
                    if self.stop_event.is_set():
                        break
                    failure_reason = "camera_read_failed"
                    raise RuntimeError("摄像头视频中断；本次片段已结束，正在等待自动重连")
                timestamp = time.monotonic()
                vessels = observation_context = None
                try:
                    vessels, observation_context = detections.get_nowait()
                except queue.Empty:
                    pass
                recording_manager.process_frame(image, camera_id=session["camera_id"], session_id=session["session_id"],
                    source_name=settings.live_camera_name, fps=fps, timestamp=timestamp, vessels=vessels,
                    source_type="camera", observation_context=observation_context)
                if frame_index % frame_step == 0:
                    _replace_latest(frames, (frame_index, image.copy(), datetime.now(timezone.utc), captured_context))
                frame_index += 1
                with self.lock:
                    self._status["frame_id"] = frame_index
            if self._inference_error:
                failure_reason = "inference_failed"
                raise RuntimeError(self._inference_error)
        except Exception as exc:
            failure = safe_camera_error(exc, settings)
            with self.lock:
                self._status.update(status="failed", error=failure)
        finally:
            self.stop_event.set()
            self._zoom_observation_stop.set()
            if capture is not None:
                capture.release()
            if self.inference_thread and self.inference_thread.is_alive():
                self.inference_thread.join(timeout=10)
            if self.zoom_observation_thread and self.zoom_observation_thread.is_alive():
                self.zoom_observation_thread.join(timeout=self.ZOOM_OBSERVATION_JOIN_TIMEOUT)
            if self._inference_error:
                failure = self._inference_error
                failure_reason = "inference_failed"
                with self.lock:
                    self._status.update(status="failed", error=failure)
            try:
                if failure:
                    recording_manager.fail(failure)
                else:
                    recording_manager.finish(reason="source_stopped")
            except Exception as exc:
                failure_reason = "recording_error"
                failure = safe_camera_error(exc, settings)
                with self.lock:
                    self._status.update(status="failed", error=failure)
            with self.lock:
                if self._status["status"] not in {"failed", "stopped"}:
                    self._status["status"] = "stopped"
                record_event("camera_failed" if self._status["status"] == "failed" else "camera_stopped",
                    camera_id=self._status["camera_id"], session_id=self._status["session_id"],
                    source_type="camera", frame_count=self._status["frame_id"],
                    reason=failure_reason if self._status["status"] == "failed" else "source_stopped")
            self._publish({"type": "camera_status", "data": self.snapshot()}, loop)
        return failure, failure_reason if failure else "source_stopped"

    def _observe_zoom(self, settings: Settings, stopped: threading.Event) -> None:
        """Refresh calibrated idle magnification without owning lens movement."""
        configured = bool(settings.camera_onvif_ptz_url and settings.camera_onvif_profile_token
                          and settings.camera_onvif_zoom_calibration)
        last_observation = object()
        while not stopped.is_set() and not self.stop_event.is_set():
            if not recording_manager.optical_control_busy():
                context = recording_manager.zoom_observation_context()
                factor = None
                reason = "zoom_unknown"
                if configured:
                    try:
                        factor = onvif_control.read_magnification()
                        reason = "zoom_observed" if factor is not None else "zoom_unknown"
                    except Exception:
                        # A transient failure is not a permanent admission
                        # failure. Keep the view unknown and retry at the next
                        # bounded interval instead of assuming a 1x position.
                        reason = "zoom_read_failed"
                current_context = recording_manager.zoom_observation_context()
                if (not stopped.is_set() and not self.stop_event.is_set()
                        and not recording_manager.optical_control_busy()
                        and current_context.get("generation") == context.get("generation")):
                    recording_manager.observe_zoom_factor(factor, context.get("generation"))
                    observation = (factor, reason)
                    if observation != last_observation:
                        record_event("zoom_observation", camera_id=self._status["camera_id"],
                            session_id=self._status["session_id"], source_type="camera",
                            generation=context.get("generation"), zoom_factor=factor, reason=reason)
                        last_observation = observation
            if stopped.wait(self.ZOOM_OBSERVATION_INTERVAL):
                return

    def _inference_worker(self, *args) -> None:
        try:
            self._infer_loop(*args)
        except Exception as exc:
            with self.lock:
                self._inference_error = safe_camera_error(exc, args[1])
            self.stop_event.set()

    def _infer_loop(self, service, settings, session, frames, detections, loop) -> None:
        storage = aggregator = repository = None
        result_uri = None
        processed = 0
        last_detection_at = None
        last_generation = None
        processor = RealtimeTargetProcessor()
        try:
            storage = ResultStorage(settings.result_root, f"live-{uuid.uuid4().hex}")
            source_name = settings.live_camera_name
            stem = storage.source_stem(1, source_name)
            repository = ResultRepository(SessionLocal)
            tracker = TrackTrackTracker(det_thresh=.45, init_thresh=.45, match_thresh=.8, max_time_lost=30,
                                        min_len=1, min_box_area=800, class_agnostic=True)
            aggregator = InstanceAggregator(storage.task_id, source_name, settings.track_confirm_hits,
                                            settings.track_lost_seconds, settings.track_finish_seconds)
            while not self.stop_event.is_set():
                try:
                    source_index, image, observed_at, observation_context = frames.get(timeout=.2)
                except queue.Empty:
                    continue
                infer_started = time.monotonic()
                result = service.infer(image, task_id=session["session_id"], source_filename=source_name,
                    task=TaskKind.REGION_DETECTION, observed_at=observed_at, frame_index=source_index, source_frame_index=source_index)
                ensure_detection_available(result)
                generation = observation_context.get("generation") if observation_context else None
                if generation != last_generation:
                    last_detection_at = None
                    last_generation = generation
                if self.stop_event.is_set():
                    break
                tracks = Pipeline._tracks_for_frame(tracker, result, image, processed + 1)
                for vessel in result.vessels:
                    match = Pipeline._best_track(vessel.ship.xyxy, tracks)
                    if match:
                        vessel.track_id = int(match.track_id)
                context = observation_context
                latest_context = recording_manager.zoom_observation_context()
                if context and context.get("generation") != latest_context.get("generation"):
                    context = {**context, "accept_detections": False}
                processor.process(service, result, image, observed_at.timestamp(),
                    RecordingSettings.model_validate(recording_manager.get_settings()), context)
                detection_at = time.monotonic()
                result.processing_ms = (detection_at - infer_started) * 1000
                elapsed = detection_at - (last_detection_at if last_detection_at is not None else infer_started)
                # Include admission and detailed models in the latency bound.
                # Moving-lens samples cannot provide entry/exit evidence.
                validate_recording_interval(session["frame_step"], session["fps"],
                    inference_seconds=elapsed if not context or context.get("accept_detections") else None)
                last_detection_at = detection_at
                current, active_ids = {}, set()
                for vessel in result.vessels:
                    if vessel.track_id is not None:
                        active_ids.add(vessel.track_id)
                        current[vessel.track_id] = aggregator.update(vessel.track_id, vessel, result.observed_at)
                live = self.live_frame(result, session["camera_id"], current)
                _replace_latest(detections, (live["vessels"], observation_context))
                result_uri = storage.relative_uri(storage.append_video_record(stem, result))
                repository.save_realtime_frame(result, session["session_id"], "camera", source_name, session["camera_id"], result_uri)
                for track_id in active_ids:
                    repository.save_realtime_instance(aggregator.snapshot(track_id, result.observed_at), session["session_id"], "camera", session["camera_id"], result_uri)
                for instance in aggregator.sweep(active_ids, result.observed_at):
                    storage.append_video_record(stem, instance)
                    repository.save_video_instance(instance, result_uri)
                    repository.save_realtime_instance(instance, session["session_id"], "camera", session["camera_id"], result_uri)
                ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
                if ok:
                    self._publish({"type": "camera_frame", "data": {"session_id": session["session_id"],
                        "frame_id": source_index, "camera_id": session["camera_id"],
                        "image": "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii"), "frame": live}}, loop)
                processed += 1
        except Exception as exc:
            with self.lock:
                self._inference_error = safe_camera_error(exc, settings)
            self.stop_event.set()
        finally:
            if result_uri and storage is not None and aggregator is not None and repository is not None:
                try:
                    for instance in aggregator.finish_all(datetime.now(timezone.utc)):
                        storage.append_video_record(stem, instance)
                        repository.save_video_instance(instance, result_uri)
                        repository.save_realtime_instance(instance, session["session_id"], "camera", session["camera_id"], result_uri)
                except Exception as exc:
                    with self.lock:
                        self._inference_error = safe_camera_error(exc, settings)


class OnvifControl:
    """Manual lens control and calibrated, absolute optical zoom movements."""
    PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
    IMAGING = "http://www.onvif.org/ver20/imaging/wsdl"
    SCHEMA = "http://www.onvif.org/ver10/schema"

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._cancel_move = threading.Event()
        self._options_cache: tuple | None = None

    def cancel_move(self) -> None:
        self._cancel_move.set()

    def available_magnifications(self) -> list[float]:
        return sorted(self._calibration(get_effective_settings()))

    def read_magnification(self) -> float | None:
        """Resolve a stationary native lens position to a measured zoom stop."""
        settings = get_effective_settings()
        if not self.configured(settings):
            return None
        calibration = self._calibration(settings)
        position, movement, reported_space = self._read_zoom_status(settings)
        if movement not in {None, "IDLE"}:
            return None
        options = self._zoom_options(settings)
        self._validate_positions(calibration, options)
        if reported_space and reported_space != options["space"]:
            return None
        # Do not guess between stops: an arbitrary native coordinate is not
        # a known optical magnification. Match only an unambiguous calibration.
        tolerance = settings.camera_onvif_zoom_tolerance * (options["max"] - options["min"])
        matches = [factor for factor, native in calibration.items()
                   if abs(native - position) <= tolerance]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _calibration(settings: Settings) -> dict[float, float]:
        try:
            document = json.loads(settings.camera_onvif_zoom_calibration)
            if not isinstance(document, dict):
                raise ValueError
            calibration = {float(factor): float(position) for factor, position in document.items()}
            if len(calibration) != len(document) or len(calibration) < 2 or 1. not in calibration:
                raise ValueError
            if not all(math.isfinite(factor) and factor >= 1 and math.isfinite(position) for factor, position in calibration.items()):
                raise ValueError
            positions = [calibration[factor] for factor in sorted(calibration)]
            differences = [b - a for a, b in zip(positions, positions[1:])]
            if not all(value > 0 for value in differences) and not all(value < 0 for value in differences):
                raise ValueError
            return calibration
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("需要配置真实光学倍率标定 CAMERA_ONVIF_ZOOM_CALIBRATION，包含 1× 和至少一个放大倍率，且镜头位置须单调且不重复") from exc

    def validate_cycle(self, factor: float) -> None:
        """Validate local calibration without sending a physical movement."""
        settings = get_effective_settings()
        if not self.configured(settings):
            raise ValueError("光学循环录制需要配置 ONVIF PTZ 地址和媒体 Profile Token")
        calibration = self._calibration(settings)
        if not math.isfinite(factor) or factor <= 1 or factor not in calibration:
            raise ValueError(f"光学倍率 {factor:g}× 未标定，请配置该倍率对应的镜头位置；不会用数字裁剪或估算位置替代")

    def _zoom_options(self, settings: Settings) -> dict:
        """Resolve the selected profile's declared absolute, non-digital space."""
        key = (settings.camera_onvif_ptz_url, settings.camera_onvif_profile_token,
               settings.camera_onvif_ptz_configuration_token, settings.camera_onvif_zoom_space_uri,
               settings.camera_onvif_username, settings.camera_onvif_password)
        if self._options_cache and self._options_cache[0] == key and time.monotonic() - self._options_cache[1] < 30:
            return dict(self._options_cache[2])
        configuration = settings.camera_onvif_ptz_configuration_token
        if not configuration:
            document = self._request(settings, self.PTZ, "GetCompatibleConfigurations",
                f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>")
            candidates = {element.attrib["token"] for element in document.iter()
                          if element.tag.rsplit("}", 1)[-1] == "PTZConfiguration" and element.attrib.get("token")}
            if len(candidates) != 1:
                raise ValueError("无法唯一确定 PTZ 配置，请配置 CAMERA_ONVIF_PTZ_CONFIGURATION_TOKEN")
            configuration = candidates.pop()
        document = self._request(settings, self.PTZ, "GetConfigurationOptions",
            f"<t:ConfigurationToken>{escape(configuration)}</t:ConfigurationToken>")
        spaces = []
        for element in document.findall(f".//{{{self.SCHEMA}}}AbsoluteZoomPositionSpace"):
            uri = element.findtext(f"{{{self.SCHEMA}}}URI") or element.findtext(f"{{{self.SCHEMA}}}SpaceURI")
            if not uri or "digital" in uri.lower():
                continue
            if settings.camera_onvif_zoom_space_uri and uri != settings.camera_onvif_zoom_space_uri:
                continue
            try:
                low = float(element.findtext(f"{{{self.SCHEMA}}}XRange/{{{self.SCHEMA}}}Min", "nan"))
                high = float(element.findtext(f"{{{self.SCHEMA}}}XRange/{{{self.SCHEMA}}}Max", "nan"))
            except ValueError:
                continue
            if math.isfinite(low) and math.isfinite(high) and high > low:
                spaces.append({"space": uri, "min": low, "max": high})
        if not spaces:
            raise ValueError("摄像头未声明可用的绝对光学变焦空间，不能执行指定倍率循环")
        if len(spaces) != 1:
            raise ValueError("摄像头声明多个绝对变焦空间，请配置 CAMERA_ONVIF_ZOOM_SPACE_URI")
        result = spaces[0]
        self._options_cache = (key, time.monotonic(), result)
        return dict(result)

    def _validate_positions(self, calibration: dict[float, float], options: dict) -> None:
        if any(position < options["min"] or position > options["max"] for position in calibration.values()):
            raise ValueError("倍率标定中的镜头位置超出摄像头声明的绝对变焦范围")

    def _read_zoom_status(self, settings: Settings) -> tuple[float, str | None, str | None]:
        document = self._request(settings, self.PTZ, "GetStatus",
            f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>")
        return self._parse_zoom_status(document)

    def _parse_zoom_status(self, document: ET.Element) -> tuple[float, str | None, str | None]:
        zoom = document.find(f".//{{{self.SCHEMA}}}Position/{{{self.SCHEMA}}}Zoom")
        if zoom is None:
            raise RuntimeError("摄像头未返回绝对变焦位置，无法确认指定倍率或 1× 复位")
        try:
            position = float(zoom.attrib.get("x", "nan"))
        except ValueError as exc:
            raise RuntimeError("摄像头返回无效的变焦位置") from exc
        if not math.isfinite(position):
            raise RuntimeError("摄像头返回无效的变焦位置")
        movement = document.findtext(f".//{{{self.SCHEMA}}}MoveStatus/{{{self.SCHEMA}}}Zoom")
        return position, movement.strip().upper() if movement else None, zoom.attrib.get("space")

    def _stop_zoom(self, settings: Settings) -> None:
        self._request(settings, self.PTZ, "Stop",
            f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken><t:PanTilt>false</t:PanTilt><t:Zoom>true</t:Zoom>")

    def move_to(self, factor: float) -> dict:
        """Move only the lens; return only after a calibrated position settles."""
        settings = get_effective_settings()
        self.validate_cycle(factor if factor != 1 else max(self._calibration(settings)))
        calibration = self._calibration(settings)
        if factor not in calibration:
            raise ValueError("请求的光学倍率未标定")
        with self.lock:
            self._cancel_move.clear()
            options = self._zoom_options(settings)
            self._validate_positions(calibration, options)
            target = calibration[factor]
            tolerance = (options["max"] - options["min"]) * settings.camera_onvif_zoom_tolerance
            position, movement, reported_space = self._read_zoom_status(settings)
            if reported_space and reported_space != options["space"]:
                raise RuntimeError("摄像头状态使用了不同的变焦坐标空间，无法核对倍率")
            if abs(position - target) <= tolerance and movement == "IDLE":
                return {"factor": factor, "position": position, "settled": True}
            profile = f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>"
            body = profile + f'<t:Position><tt:Zoom x="{target:.12g}" space="{escape(options["space"], {chr(34): "&quot;"})}"/></t:Position>'
            deadline = time.monotonic() + settings.camera_onvif_zoom_timeout
            stable_reads = 0
            try:
                self._request(settings, self.PTZ, "AbsoluteMove", body)
                while time.monotonic() < deadline:
                    if self._cancel_move.is_set():
                        raise RuntimeError("光学变焦动作已取消")
                    position, movement, space = self._read_zoom_status(settings)
                    if self._cancel_move.is_set():
                        raise RuntimeError("光学变焦动作已取消")
                    if space and space != options["space"]:
                        raise RuntimeError("摄像头状态使用了不同的变焦坐标空间，无法核对倍率")
                    at_target = abs(position - target) <= tolerance
                    stable_reads = stable_reads + 1 if at_target else 0
                    if at_target and (movement == "IDLE" or movement is None and stable_reads >= 2):
                        return {"factor": factor, "position": position, "settled": True}
                    self._cancel_move.wait(.1)
                raise RuntimeError(f"摄像头未在限定时间到达光学 {factor:g}×，录制循环已暂停")
            except Exception:
                try:
                    self._stop_zoom(settings)
                except Exception:
                    pass
                raise

    @staticmethod
    def configured(settings: Settings) -> bool:
        return bool(settings.camera_onvif_ptz_url and settings.camera_onvif_profile_token)

    def _request(self, settings: Settings, namespace: str, action: str, body: str) -> ET.Element:
        is_ptz = namespace == self.PTZ
        endpoint = settings.camera_onvif_ptz_url if is_ptz else settings.camera_onvif_imaging_url
        if not endpoint:
            raise ValueError("未配置对应的 ONVIF 服务地址")
        created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        nonce = os.urandom(16)
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + settings.camera_onvif_password.encode()).digest()).decode()
        security = ""
        if settings.camera_onvif_username:
            security = ('<wsse:Security s:mustUnderstand="1"><wsse:UsernameToken>'
                f'<wsse:Username>{escape(settings.camera_onvif_username)}</wsse:Username>'
                '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
                f'{digest}</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
                f'{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created>'
                '</wsse:UsernameToken></wsse:Security>')
        envelope = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:t="{namespace}" xmlns:tt="http://www.onvif.org/ver10/schema" '
            'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
            'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f'<s:Header>{security}</s:Header><s:Body><t:{action}>{body}</t:{action}></s:Body></s:Envelope>')
        auth = httpx.DigestAuth(settings.camera_onvif_username, settings.camera_onvif_password) if settings.camera_onvif_username else None
        with httpx.Client(timeout=5, auth=auth, follow_redirects=False, trust_env=False) as client:
            response = client.post(endpoint, content=envelope.encode(), headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{namespace}/{action}"'})
        # Parse faults before raise_for_status: cameras commonly use HTTP 500
        # for a useful ONVIF Unsupported/NotAuthorized SOAP fault.
        try:
            document = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise RuntimeError(f"ONVIF 返回无效响应 (HTTP {response.status_code})") from exc
        fault = document.find(".//{http://www.w3.org/2003/05/soap-envelope}Fault")
        if fault is not None:
            reason = fault.findtext(".//{http://www.w3.org/2003/05/soap-envelope}Text") or "设备不支持或拒绝了此操作"
            raise RuntimeError(safe_camera_error(reason, settings))
        response.raise_for_status()
        return document

    def _autofocus_supported(self, settings: Settings) -> bool:
        if not settings.camera_onvif_imaging_url or not settings.camera_onvif_source_token:
            return False
        options = self._request(settings, self.IMAGING, "GetOptions",
            f"<t:VideoSourceToken>{escape(settings.camera_onvif_source_token)}</t:VideoSourceToken>")
        return any(element.tag.rsplit("}", 1)[-1] == "AutoFocusModes" and (element.text or "").strip().upper() == "AUTO"
                   for element in options.iter())

    def status(self) -> dict:
        settings = get_effective_settings()
        configured = self.configured(settings)
        response = {"configured": configured, "supported": False, "focus_supported": False,
                    "autofocus_supported": False, "focus_error": None,
                    "absolute_zoom_supported": False, "calibrated": False,
                    "magnifications": [], "cycle_supported": False, "zoom_error": None, "error": None,
                    "measured_zoom_factor": None}
        if not configured:
            response.update(error="未配置 ONVIF PTZ 服务及媒体 Profile Token", zoom_error="光学循环录制尚未配置摄像头")
            return response
        try:
            status = self._request(settings, self.PTZ, "GetStatus", f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>")
            nodes = self._request(settings, self.PTZ, "GetNodes", "")
            zoom_supported = nodes.find(".//{http://www.onvif.org/ver10/schema}ContinuousZoomVelocitySpace") is not None
            response.update(supported=zoom_supported,
                focus_supported=bool(settings.camera_onvif_imaging_url and settings.camera_onvif_source_token),
                error=None if zoom_supported else "该设备未声明 ONVIF 连续光学变焦能力")
            if response["focus_supported"]:
                try:
                    response["autofocus_supported"] = self._autofocus_supported(settings)
                    if not response["autofocus_supported"]:
                        response["focus_error"] = "设备未声明 ONVIF 自动对焦能力，可使用近焦、远焦或设备网页调整对焦"
                except Exception as exc:
                    response["focus_error"] = safe_camera_error(exc, settings)
            try:
                options = self._zoom_options(settings)
                response["absolute_zoom_supported"] = True
                calibration = self._calibration(settings)
                self._validate_positions(calibration, options)
                position, movement, reported_space = self._parse_zoom_status(status)
                if reported_space and reported_space != options["space"]:
                    raise ValueError("摄像头状态使用了不同的变焦坐标空间，无法核对倍率")
                response.update(calibrated=True, magnifications=sorted(calibration), cycle_supported=True)
                tolerance = settings.camera_onvif_zoom_tolerance * (options["max"] - options["min"])
                matches = [factor for factor, native in calibration.items()
                           if abs(native - position) <= tolerance]
                if movement in {None, "IDLE"} and len(matches) == 1:
                    response["measured_zoom_factor"] = matches[0]
            except Exception as exc:
                response["zoom_error"] = safe_camera_error(exc, settings)
            return response
        except Exception as exc:
            response.update(error=safe_camera_error(exc, settings), zoom_error=safe_camera_error(exc, settings))
            return response

    def command(self, action: str) -> dict:
        settings = get_effective_settings()
        if not self.configured(settings):
            raise ValueError("摄像头光学控制尚未配置；请配置 ONVIF PTZ 服务和媒体 Profile Token")
        profile = f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>"
        source = f"<t:VideoSourceToken>{escape(settings.camera_onvif_source_token)}</t:VideoSourceToken>"
        if action == "stop":
            self.cancel_move()
        with self.lock:
            if action in {"zoom_in", "zoom_out"}:
                velocity = .35 if action == "zoom_in" else -.35
                self._request(settings, self.PTZ, "ContinuousMove", profile + f'<t:Velocity><tt:Zoom x="{velocity}"/></t:Velocity><t:Timeout>PT0.4S</t:Timeout>')
            elif action == "home":
                self._request(settings, self.PTZ, "GotoHomePosition", profile)
            elif action == "stop":
                self._request(settings, self.PTZ, "Stop", profile + "<t:PanTilt>true</t:PanTilt><t:Zoom>true</t:Zoom>")
                if settings.camera_onvif_imaging_url and settings.camera_onvif_source_token:
                    self._request(settings, self.IMAGING, "Stop", source)
            elif action in {"focus_near", "focus_far", "autofocus"}:
                if not settings.camera_onvif_imaging_url or not settings.camera_onvif_source_token:
                    raise ValueError("手动/自动对焦需要配置 ONVIF Imaging 地址和 VideoSource Token")
                if action == "autofocus":
                    if not self._autofocus_supported(settings):
                        raise ValueError("摄像头未声明 ONVIF 自动对焦能力，请使用近焦、远焦或设备网页调整对焦")
                    self._request(settings, self.IMAGING, "SetImagingSettings", source +
                        "<t:ImagingSettings><tt:Focus><tt:AutoFocusMode>AUTO</tt:AutoFocusMode></tt:Focus></t:ImagingSettings><t:ForcePersistence>false</t:ForcePersistence>")
                else:
                    velocity = -.35 if action == "focus_near" else .35
                    try:
                        self._request(settings, self.IMAGING, "Move", source +
                            f"<t:Focus><tt:Continuous><tt:Speed>{velocity}</tt:Speed></tt:Continuous></t:Focus>")
                        time.sleep(.35)
                    finally:
                        self._request(settings, self.IMAGING, "Stop", source)
            else:
                raise ValueError("不支持的摄像头控制操作")
        return {"accepted": True, "action": action}


onvif_control = OnvifControl()
