"""All HTTP/WebSocket entry points for the v2 backend."""
from __future__ import annotations

import asyncio
import base64
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
from .models import ShipArchive, VideoResult
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
        redis = "unavailable"
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
    return {
        "status": "healthy" if database == "ready" else "degraded",
        "environment": settings.app_env,
        "device": settings.device,
        "dependencies": {"database": database, "redis": redis},
        "models": models,
    }


@router.get("/api/dashboard", tags=["system"])
def dashboard():
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        rows = list(db.query(VideoResult).filter(VideoResult.end_time >= day_start).order_by(VideoResult.end_time.desc()).all())
        mmsi = {row.ais_mmsi for row in rows if row.ais_mmsi}
        archives = {row.mmsi: row for row in db.query(ShipArchive).filter(ShipArchive.mmsi.in_(mmsi)).all()} if mmsi else {}
    drafts = [row.draft_depth_m for row in rows if row.draft_depth_m is not None]
    hourly: dict[str, list[VideoResult]] = {}
    for row in rows:
        hour = row.end_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        hourly.setdefault(hour, []).append(row)
    trend = [{"time": hour, "traffic": len(items), "average_draft": round(sum(values) / len(values), 4) if (values := [item.draft_depth_m for item in items if item.draft_depth_m is not None]) else None} for hour, items in sorted(hourly.items())]
    return {"statistics": {"generated_at": now.isoformat(), "today_traffic": len(rows), "overload_alerts": 0, "average_draft": round(sum(drafts) / len(drafts), 4) if drafts else None, "average_displacement": None, "hourly": trend}, "records": [{"id": row.id, "captured_at": row.end_time.isoformat(), "track_id": row.instance_id, "camera_id": "local-video", "ship_name": row.recognized_zh, "mmsi": row.ais_mmsi, "draft_depth": row.draft_depth_m, "review_status": row.status} for row in rows[:12]]}


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
                for instance in aggregator.sweep(active_ids, result.observed_at):
                    storage.append_video_record(stem, instance)
                    repository.save_video_instance(instance, result_uri)
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
