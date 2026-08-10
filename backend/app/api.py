"""All HTTP/WebSocket entry points for the v2 backend."""
from __future__ import annotations

import asyncio
import base64
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from redis import Redis
from sqlalchemy import text

from .config import get_effective_settings, get_settings
from .jobs import job_manager
from .inference.frame_inference import FrameInferenceService
from .inference.model_registry import ModelRegistry
from .pipeline import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, Pipeline
from .schemas import InstanceResultV2, TaskKind
from .database import ResultRepository, SessionLocal, engine
from .models import RealtimeTrackingResult, RealtimeVideoResult
from .storage import ResultStorage
from .tracking import InstanceAggregator, TrackTrackTracker

router = APIRouter()


@router.get("/api/health", tags=["system"])
def health():
    settings = get_effective_settings()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database = "ready"
    except Exception:
        database = "unavailable"
    try:
        Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1).ping()
        redis = "ready"
    except Exception:
        # Redis is only needed by an external queue-based worker.  The API,
        # WebSocket hub and local-video playback remain available without it.
        redis = "optional"
    model_paths = (
        ("ship_detection", settings.ship_detector_config, settings.ship_detector_weights),
        ("draft_multitask", settings.draftformer_config, settings.draftformer_weights),
        ("ship_name_recognition", settings.shipname_config, settings.shipname_weights),
    )
    models = [
        {
            "name": name,
            "status": "ready" if config and config.is_file() and weights and weights.is_file() else "unavailable",
            "config": str(config) if config else None,
            "weights": str(weights) if weights else None,
        }
        for name, config, weights in model_paths
    ]
    models_ready = all(model["status"] == "ready" for model in models)
    return {
        "status": "healthy" if database == "ready" and models_ready else "degraded",
        "environment": settings.app_env,
        "device": settings.device,
        "realtime_status": "ready",
        "dependencies": {"database": database, "redis": redis},
        "models": models,
    }


@router.get("/api/streams", tags=["system"])
def streams():
    """Expose the configured live-monitoring source for the status UI."""
    settings = get_effective_settings()
    configured = bool(settings.live_stream_url)
    return {
        "items": [{
            "id": settings.live_camera_id,
            "name": settings.live_camera_name,
            "status": "configured" if configured else "unconfigured",
            "protocol": settings.live_stream_protocol if configured else None,
            "play_url": settings.live_stream_url or None,
        }]
    }


@router.get("/api/dashboard", tags=["system"])
def dashboard():
    """Live-monitoring statistics are based on tracked instances, not the ship archive."""
    local_timezone = ZoneInfo("Asia/Shanghai")
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(local_timezone)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    with SessionLocal() as db:
        rows = list(db.query(RealtimeTrackingResult).filter(RealtimeTrackingResult.start_time >= day_start).order_by(RealtimeTrackingResult.start_time.desc()).all())

    drafts = [row.draft_depth_m for row in rows if row.draft_depth_m is not None]
    hourly: dict[datetime, list[RealtimeTrackingResult]] = {}
    for row in rows:
        recorded_at = row.start_time if row.start_time.tzinfo else row.start_time.replace(tzinfo=timezone.utc)
        hour = recorded_at.astimezone(local_timezone).replace(minute=0, second=0, microsecond=0)
        hourly.setdefault(hour, []).append(row)
    trend = []
    for offset in range(23, -1, -1):
        hour = local_now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=offset)
        items = hourly.get(hour, [])
        values = [item.draft_depth_m for item in items if item.draft_depth_m is not None]
        trend.append({"time": hour.isoformat(), "traffic": len(items), "average_draft": round(sum(values) / len(values), 4) if values else None})
    displacements = [row.displacement for row in rows if row.displacement is not None]
    overload_alerts = sum(row.draft_depth_m > 4.6 for row in rows if row.draft_depth_m is not None)
    return {"statistics": {"generated_at": now.isoformat(), "today_traffic": len(rows), "overload_alerts": overload_alerts, "average_draft": round(sum(drafts) / len(drafts), 4) if drafts else None, "average_displacement": round(sum(displacements) / len(displacements), 2) if displacements else None, "hourly": trend}, "records": [{"id": row.id, "captured_at": row.start_time.isoformat(), "track_id": row.instance_id, "camera_id": row.camera_id or "realtime", "ship_name": row.recognized_zh, "mmsi": row.ais_mmsi, "draft_depth": row.draft_depth_m, "displacement_tons": row.displacement, "review_status": row.status} for row in rows[:12]]}


@router.get("/api/realtime/results", tags=["realtime"])
def realtime_results(limit: int = 50):
    """Return persisted live frame and instance results for dashboard recovery/refresh."""
    limit = max(1, min(limit, 500))
    with SessionLocal() as db:
        frames = list(db.query(RealtimeVideoResult).order_by(RealtimeVideoResult.observed_at.desc()).limit(limit).all())
        instances = list(db.query(RealtimeTrackingResult).order_by(RealtimeTrackingResult.end_time.desc()).limit(limit).all())
    frame_items = []
    for row in frames:
        vessels = (row.payload_json or {}).get("vessels") or []
        first = vessels[0] if vessels else {}
        draft = first.get("draft") or {}
        ship = first.get("ship") or {}
        frame_items.append({"id": row.id, "session_id": row.session_id, "source_type": row.source_type, "source_name": row.source_name, "camera_id": row.camera_id, "track_id": first.get("track_id") or first.get("vessel_id"), "frame_index": row.frame_index, "captured_at": row.observed_at.isoformat(), "ship_name": first.get("recognized_zh", "UNKNOWN"), "mmsi": None, "draft_depth": draft.get("depth_m"), "status": "pending_review", "confidence": ship.get("confidence"), "vessel_count": row.vessel_count})
    return {
        "frames": frame_items,
        "instances": [{"id": row.id, "session_id": row.session_id, "source_type": row.source_type, "source_name": row.source_name, "camera_id": row.camera_id, "track_id": row.instance_id, "captured_at": row.end_time.isoformat(), "start_time": row.start_time.isoformat(), "ship_name": row.recognized_zh, "mmsi": row.ais_mmsi, "draft_depth": row.draft_depth_m, "status": row.status} for row in instances],
    }


def _job(task_id: str) -> dict:
    try:
        return job_manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(404, "task not found") from exc


@router.post("/api/jobs", status_code=202, tags=["batch-jobs"])
async def create_job(files: list[UploadFile] = File(...), task: str = Form("full_pipeline"), enable_tracking: bool = Form(False), frame_step: int = Form(1), max_frames: int | None = Form(None), visualize: bool = Form(False)):
    if task not in {kind.value for kind in TaskKind}:
        raise HTTPException(422, "unsupported task")
    if not files or frame_step < 1 or max_frames is not None and max_frames < 1:
        raise HTTPException(422, "files and positive frame_step/max_frames are required")
    payloads = []
    try:
        for upload in files:
            filename = Path(upload.filename or "").name
            if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
                raise HTTPException(415, "unsupported media type")
            content = await upload.read()
            if not content:
                raise HTTPException(422, f"empty upload: {filename}")
            payloads.append((filename, content))
        return job_manager.create(payloads, {"task": task, "enable_tracking": enable_tracking, "frame_step": frame_step, "max_frames": max_frames, "visualize": visualize})
    finally:
        for upload in files:
            await upload.close()


@router.get("/api/jobs", tags=["batch-jobs"])
def list_jobs(): return {"items": job_manager.list()}


@router.get("/api/jobs/{task_id}", tags=["batch-jobs"])
def get_job(task_id: str): return _job(task_id)


@router.post("/api/jobs/{task_id}/cancel", tags=["batch-jobs"])
def cancel_job(task_id: str):
    _job(task_id)
    return job_manager.cancel(task_id)


@router.get("/api/jobs/{task_id}/files/{relative_path:path}", tags=["batch-jobs"])
def download_result(task_id: str, relative_path: str):
    _job(task_id)
    root = (job_manager.settings.result_root / task_id).resolve()
    requested = Path(relative_path)
    path = ((job_manager.settings.result_root / requested) if requested.parts and requested.parts[0] == task_id else root / requested).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "result file not found")
    return FileResponse(path)


class RealtimeHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self.lock:
            self.clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        stale = []
        for client in tuple(self.clients):
            try:
                await asyncio.wait_for(client.send_json(message), timeout=1.5)
            except Exception:
                stale.append(client)
        if stale:
            async with self.lock:
                self.clients.difference_update(stale)


hub = RealtimeHub()


def _live_frame(result, camera_id: str, current_by_track: dict[int, dict]) -> dict:
    """Translate the v2 frame model to the frontend's realtime display contract."""
    vessels = []
    for vessel in result.vessels:
        boxes = []
        for detection in [vessel.ship, *vessel.regions, *vessel.draft_marks]:
            x1, y1, x2, y2 = detection.xyxy
            boxes.append({"type": detection.label, "xywh": [x1, y1, x2 - x1, y2 - y1], "confidence": detection.confidence, "class_name": detection.class_name})
        track_id = vessel.track_id or vessel.vessel_id
        current = current_by_track.get(track_id, {})
        status = current.get("current_status", "pending_review")
        vessels.append({"ship_id": track_id, "ship_name": current.get("current_name", vessel.recognized_zh), "draft_depth_m": current.get("current_draft_depth_m", vessel.draft.depth_m), "current_status": getattr(status, "value", status), "confidence": vessel.ship.confidence, "boxes": boxes, "assets": {}})
    return {"frame_timestamp": result.observed_at.isoformat(), "frame_id": result.frame_index or 0, "camera_id": camera_id, "source_width": result.image_width, "source_height": result.image_height, "processing_ms": result.processing_ms, "vessels": vessels}


class _LocalPlayback:
    """Upload a local video and publish the same live-frame payload as a camera."""
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.models: ModelRegistry | None = None
        self.status = {"status": "idle", "session_id": None, "filename": None, "camera_id": None, "frame_id": 0, "total_frames": 0, "frame_step": 1, "error": None}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.status)

    def start(self, path: Path, camera_id: str, frame_step: int, loop: asyncio.AbstractEventLoop) -> dict:
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("local video playback is already running")
            self.stop_event.clear()
            self.status.update(status="starting", session_id=uuid.uuid4().hex, filename=path.name, camera_id=camera_id, frame_id=0, total_frames=0, frame_step=frame_step, error=None)
            self.thread = threading.Thread(target=self._run, args=(path, loop), daemon=True, name="local-video-playback")
            self.thread.start()
            return dict(self.status)

    def stop(self) -> dict:
        self.stop_event.set()
        with self.lock:
            if self.status["status"] in {"starting", "running"}:
                self.status["status"] = "stopped"
            return dict(self.status)

    def _run(self, path: Path, loop: asyncio.AbstractEventLoop) -> None:
        capture = None
        storage = None
        aggregator = None
        result_uri = None
        session_id = None
        camera_id = None
        try:
            settings = get_settings()
            if self.models is None:
                self.models = ModelRegistry(settings)
                self.models.load()
            service = FrameInferenceService(self.models)
            storage = ResultStorage(settings.result_root, f"live-{uuid.uuid4().hex}")
            stem = storage.source_stem(1, path.name)
            repository = ResultRepository(SessionLocal)
            capture = cv2.VideoCapture(str(path))
            if not capture.isOpened():
                raise ValueError("unable to read local video")
            with self.lock:
                self.status.update(status="running", total_frames=int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
                session_id, camera_id, step = self.status["session_id"], self.status["camera_id"], self.status["frame_step"]
            tracker = TrackTrackTracker(det_thresh=.45, init_thresh=.45, match_thresh=.8, max_time_lost=30, min_len=1, min_box_area=800, class_agnostic=True)
            aggregator = InstanceAggregator(storage.task_id, path.name, settings.track_confirm_hits, settings.track_lost_seconds, settings.track_finish_seconds)
            source_index = processed = 0
            while not self.stop_event.is_set():
                ok, image = capture.read()
                if not ok:
                    break
                if source_index % step:
                    source_index += 1
                    continue
                result = service.infer(image, task_id=str(session_id), source_filename=path.name, task=TaskKind.FULL_PIPELINE, observed_at=datetime.now(timezone.utc), frame_index=processed, source_frame_index=source_index)
                tracks = Pipeline._tracks_for_frame(tracker, result, image, processed + 1)
                current = {}
                active_ids = set()
                for vessel in result.vessels:
                    match = Pipeline._best_track(vessel.ship.xyxy, tracks)
                    if match:
                        vessel.track_id = int(match.track_id)
                        active_ids.add(vessel.track_id)
                        current[vessel.track_id] = aggregator.update(vessel.track_id, vessel, result.observed_at)
                result_uri = storage.relative_uri(storage.append_video_record(stem, result))
                repository.save_realtime_frame(result, str(session_id), "local_video", path.name, str(camera_id), result_uri)
                for track_id in active_ids:
                    repository.save_realtime_instance(aggregator.snapshot(track_id, result.observed_at), str(session_id), "local_video", str(camera_id), result_uri)
                for instance in aggregator.sweep(active_ids, result.observed_at):
                    storage.append_video_record(stem, instance)
                    repository.save_video_instance(instance, result_uri)
                    repository.save_realtime_instance(instance, str(session_id), "local_video", str(camera_id), result_uri)
                encoded_ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
                if encoded_ok:
                    payload = {"type": "local_playback_frame", "data": {"session_id": session_id, "frame_id": processed, "camera_id": camera_id, "image": "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii"), "frame": _live_frame(result, str(camera_id), current)}}
                    asyncio.run_coroutine_threadsafe(hub.broadcast(payload), loop).result(timeout=5)
                processed += 1; source_index += 1
                with self.lock:
                    self.status["frame_id"] = processed
            with self.lock:
                if self.status["status"] != "stopped": self.status["status"] = "completed"
        except Exception as exc:
            with self.lock:
                self.status.update(status="failed", error=str(exc))
        finally:
            if capture is not None:
                capture.release()
            if aggregator is not None and storage is not None and result_uri:
                repository = ResultRepository(SessionLocal)
                for instance in aggregator.finish_all(datetime.now(timezone.utc)):
                    storage.append_video_record(stem, instance)
                    repository.save_video_instance(instance, result_uri)
                    repository.save_realtime_instance(instance, str(session_id), "local_video", str(camera_id), result_uri)
            try:
                asyncio.run_coroutine_threadsafe(hub.broadcast({"type": "local_playback_status", "data": self.snapshot()}), loop).result(timeout=5)
            except Exception:
                pass


local_playback = _LocalPlayback()


@router.get("/api/realtime/local-video", tags=["realtime"])
def local_video_status(): return local_playback.snapshot()


@router.post("/api/realtime/local-video", status_code=202, tags=["realtime"])
async def start_local_video(file: UploadFile = File(...), frame_step: int = Form(3), camera_id: str = Form("local-video")):
    if frame_step < 1 or frame_step > 1000 or Path(file.filename or "").suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(422, "valid video file and frame_step are required")
    destination = get_settings().upload_root / "live"; destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{uuid.uuid4().hex}_{Path(file.filename or 'local.mp4').name}"
    try:
        with path.open("wb") as handle:
            while chunk := await file.read(4 * 1024 * 1024): handle.write(chunk)
        return local_playback.start(path, camera_id, frame_step, asyncio.get_running_loop())
    finally:
        await file.close()


@router.post("/api/realtime/local-video/stop", tags=["realtime"])
def stop_local_video(): return local_playback.stop()


@router.post("/api/realtime/instances", status_code=202, tags=["realtime"])
async def publish_instance_event(event: dict):
    required = {"track_id", "current_name", "current_draft_depth_m", "current_status"}
    missing = required - event.keys()
    if missing:
        raise HTTPException(422, f"missing current fields: {sorted(missing)}")
    final = event.get("final_instance")
    if final is not None:
        try:
            instance = InstanceResultV2.model_validate(final)
            result_uri = str(event.get("result_uri") or "")
            if not result_uri:
                raise ValueError("result_uri is required when final_instance is supplied")
            ResultRepository(SessionLocal).save_video_instance(instance, result_uri)
            ResultRepository(SessionLocal).save_realtime_instance(instance, instance.task_id, "realtime_stream", event.get("camera_id"), result_uri)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    await hub.broadcast({"type": "instance_update", "data": event})
    return {"accepted": True, "finalized": final is not None}


@router.websocket("/ws/realtime")
async def realtime(websocket: WebSocket):
    await hub.connect(websocket)
    try:
        await websocket.send_json({"type": "connected", "data": {"timestamp": datetime.now(timezone.utc).isoformat()}})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=20)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()})
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
import cv2
