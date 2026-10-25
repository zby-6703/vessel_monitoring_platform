"""Server-side camera capture, inference and optional ONVIF lens control.

Capture and inference have separate workers: an occupied model never prevents
the next source frame from reaching the event recorder. Inference uses a
one-frame queue so an overloaded camera cannot accumulate stale frames.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
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
from .recording import recording_manager
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
    delay = config["post_roll_seconds"]
    interval = frame_step / fps
    if interval >= delay / 2:
        raise RuntimeError(f"推理采样间隔 {interval:.2f} 秒过大，无法可靠确认过船。请减少推理跳帧，或将离场延时设置为大于 {interval * 2:.2f} 秒后重新启动")
    if inference_seconds is not None and inference_seconds >= delay:
        raise RuntimeError(f"摄像头推理结果间隔/耗时达到 {inference_seconds:.2f} 秒，已超过离场延时 {delay:.2f} 秒。请提高离场延时或优化模型运行速度后重新启动")


class CameraCapture:
    def __init__(self, broadcast: Callable, live_frame: Callable) -> None:
        self.broadcast = broadcast
        self.live_frame = live_frame
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.inference_thread: threading.Thread | None = None
        self._inference_error: str | None = None
        self._status = {"status": "idle", "session_id": None, "camera_id": None,
                        "filename": None, "frame_id": 0, "error": None}

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
            self.stop_event.clear()
            self._inference_error = None
            self._status.update(status="starting", session_id=uuid.uuid4().hex,
                                camera_id=settings.live_camera_id, filename=settings.live_camera_name,
                                frame_id=0, error=None)
            self.thread = threading.Thread(target=self._run, args=(settings, loop, frame_step or settings.live_capture_frame_step),
                                           name="camera-capture", daemon=True)
            self.thread.start()
            return self.snapshot()

    def stop(self) -> dict:
        self.stop_event.set()
        with self.lock:
            if self._status["status"] in {"starting", "running"}:
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
        capture = None
        models = None
        frames: queue.Queue = queue.Queue(maxsize=1)
        detections: queue.Queue = queue.Queue(maxsize=1)
        failure = None
        try:
            models = ModelRegistry(settings)
            models.load()
            if self.stop_event.is_set():
                return
            service = FrameInferenceService(models)
            capture = cv2.VideoCapture()
            opened = capture.open(settings.live_capture_url, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
            if not opened:
                raise RuntimeError("无法连接摄像头视频源，请检查地址、账号、网络和编码格式")
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            if not math.isfinite(fps) or fps < 1 or fps > 120:
                fps = settings.live_capture_fps
            validate_recording_interval(frame_step, fps)
            with self.lock:
                self._status["status"] = "running"
                session = dict(self._status)
                session.update(fps=fps, frame_step=frame_step)
            self.inference_thread = threading.Thread(target=self._inference_worker,
                args=(service, settings, session, frames, detections, loop), name="camera-inference", daemon=True)
            self.inference_thread.start()
            self._publish({"type": "camera_status", "data": self.snapshot()}, loop)
            frame_index = 0
            while not self.stop_event.is_set():
                ok, image = capture.read()
                if not ok:
                    if self.stop_event.is_set():
                        break
                    raise RuntimeError("摄像头视频中断；本次片段已结束，请检查连接后重新启动采集")
                timestamp = time.monotonic()
                vessels = None
                try:
                    vessels = detections.get_nowait()
                except queue.Empty:
                    pass
                recording_manager.process_frame(image, camera_id=session["camera_id"], session_id=session["session_id"],
                    source_name=settings.live_camera_name, fps=fps, timestamp=timestamp, vessels=vessels)
                if frame_index % frame_step == 0:
                    _replace_latest(frames, (frame_index, image.copy(), datetime.now(timezone.utc)))
                frame_index += 1
                with self.lock:
                    self._status["frame_id"] = frame_index
            if self._inference_error:
                raise RuntimeError(self._inference_error)
        except Exception as exc:
            failure = safe_camera_error(exc, settings)
            with self.lock:
                self._status.update(status="failed", error=failure)
        finally:
            self.stop_event.set()
            if capture is not None:
                capture.release()
            if self.inference_thread and self.inference_thread.is_alive():
                self.inference_thread.join(timeout=10)
            if self._inference_error and not failure:
                failure = self._inference_error
                with self.lock:
                    self._status.update(status="failed", error=failure)
            try:
                if failure:
                    recording_manager.fail(failure)
                else:
                    recording_manager.finish(reason="source_stopped")
            except Exception as exc:
                with self.lock:
                    self._status.update(status="failed", error=safe_camera_error(exc, settings))
            with self.lock:
                if self._status["status"] not in {"failed", "stopped"}:
                    self._status["status"] = "stopped"
            # A still-running model belongs to the inference worker until it
            # exits; do not release its tensors underneath a model call.
            if models is not None and not (self.inference_thread and self.inference_thread.is_alive()):
                models.release()
            self._publish({"type": "camera_status", "data": self.snapshot()}, loop)

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
                    source_index, image, observed_at = frames.get(timeout=.2)
                except queue.Empty:
                    continue
                infer_started = time.monotonic()
                result = service.infer(image, task_id=session["session_id"], source_filename=source_name,
                    task=TaskKind.FULL_PIPELINE, observed_at=observed_at, frame_index=source_index, source_frame_index=source_index)
                ensure_detection_available(result)
                detection_at = time.monotonic()
                elapsed = detection_at - (last_detection_at if last_detection_at is not None else infer_started)
                validate_recording_interval(session["frame_step"], session["fps"], inference_seconds=elapsed)
                last_detection_at = detection_at
                if self.stop_event.is_set():
                    break
                tracks = Pipeline._tracks_for_frame(tracker, result, image, processed + 1)
                current, active_ids = {}, set()
                for vessel in result.vessels:
                    match = Pipeline._best_track(vessel.ship.xyxy, tracks)
                    if match:
                        vessel.track_id = int(match.track_id)
                        active_ids.add(vessel.track_id)
                        current[vessel.track_id] = aggregator.update(vessel.track_id, vessel, result.observed_at)
                live = self.live_frame(result, session["camera_id"], current)
                _replace_latest(detections, live["vessels"])
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
    """Small manual ONVIF adapter; no uncalibrated autonomous camera movement."""
    PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
    IMAGING = "http://www.onvif.org/ver20/imaging/wsdl"

    def __init__(self) -> None:
        self.lock = threading.Lock()

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

    def status(self) -> dict:
        settings = get_effective_settings()
        configured = self.configured(settings)
        if not configured:
            return {"configured": False, "supported": False, "error": "未配置 ONVIF PTZ 服务及媒体 Profile Token"}
        try:
            self._request(settings, self.PTZ, "GetStatus", f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>")
            nodes = self._request(settings, self.PTZ, "GetNodes", "")
            zoom_supported = nodes.find(".//{http://www.onvif.org/ver10/schema}ContinuousZoomVelocitySpace") is not None
            return {"configured": True, "supported": zoom_supported,
                    "focus_supported": bool(settings.camera_onvif_imaging_url and settings.camera_onvif_source_token),
                    "error": None if zoom_supported else "该设备未声明 ONVIF 连续光学变焦能力"}
        except Exception as exc:
            return {"configured": True, "supported": False, "error": safe_camera_error(exc, settings)}

    def command(self, action: str) -> dict:
        settings = get_effective_settings()
        if not self.configured(settings):
            raise ValueError("摄像头光学控制尚未配置；请配置 ONVIF PTZ 服务和媒体 Profile Token")
        profile = f"<t:ProfileToken>{escape(settings.camera_onvif_profile_token)}</t:ProfileToken>"
        source = f"<t:VideoSourceToken>{escape(settings.camera_onvif_source_token)}</t:VideoSourceToken>"
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
