"""Serial batch queue and job.json state for the v2 data pipeline."""
from __future__ import annotations

import json
import queue
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .schemas import JobStatus, TaskKind
from .database import ResultRepository, SessionLocal
from .inference.frame_inference import FrameInferenceService
from .inference.model_registry import ModelRegistry
from .pipeline import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, Pipeline
from .tracking import TrackTrackTracker
from .storage import ResultStorage
from .inference.scheduler import inference_scheduler


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    """Persistent serial GPU queue; each job is represented only by job.json."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.result_root.mkdir(parents=True, exist_ok=True)
        self.settings.upload_root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, dict] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._models: ModelRegistry | None = None
        self.recordings = None
        self._cancel: dict[str, threading.Event] = {}
        self._load()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._consume, daemon=True, name="v2-batch-pipeline")
        self._thread.start()

    def stop(self) -> None:
        for event in self._cancel.values():
            event.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=30)
        if self._models:
            self._models.release()
        self._models = None

    def create(self, files: list[tuple[str, bytes | Path]], options: dict[str, Any], *, source_recording: dict | None = None) -> dict:
        task_id = uuid.uuid4().hex
        task = TaskKind(options["task"])
        ResultStorage(self.settings.result_root, task_id)
        upload_dir = self.settings.upload_root / task_id
        upload_dir.mkdir(parents=True, exist_ok=False)
        items = []
        for ordinal, (filename, content) in enumerate(files, 1):
            safe = Path(filename).name or f"upload-{ordinal}"
            saved = upload_dir / f"{ordinal:03d}_{safe}"
            if isinstance(content, Path):
                try:
                    os.link(content, saved)
                except OSError:
                    shutil.copyfile(content, saved)
            else:
                saved.write_bytes(content)
            items.append({"filename": safe, "upload_name": saved.name, "status": "queued", "progress": 0, "error": None, "result_uri": None, "visual_uri": None})
        job = {
            "id": task_id,
            "status": JobStatus.QUEUED.value,
            "created_at": utc_now(),
            "started_at": None,
            "completed_at": None,
            "error": None,
            "options": {
                "task": task.value,
                "enable_tracking": bool(options.get("enable_tracking", False)),
                "frame_step": int(options.get("frame_step", 1)),
                "max_frames": options.get("max_frames"),
                "visualize": bool(options.get("visualize", False)),
            },
            "items": items,
            "progress": 0,
            "source_recording": source_recording,
        }
        with self._lock:
            self.jobs[task_id] = job
            self._cancel[task_id] = threading.Event()
            self._write(job)
        self._queue.put(task_id)
        return self.get(task_id)

    def bind_recordings(self, manager):
        self.recordings = manager
        for job in sorted(self.jobs.values(), key=lambda value: value["created_at"]):
            self._sync_recording(job)
        manager.reconcile_analysis(set(self.jobs))

    def create_from_recording(self, manager, recording_id: str, options: dict) -> dict:
        # Lock order is always jobs -> recordings; duplicate clicks cannot
        # enqueue two active analyses of the same immutable passage clip.
        with self._lock:
            for job in self.jobs.values():
                if (job.get("source_recording") or {}).get("id") == recording_id and job["status"] in {"queued", "running"}:
                    return self.get(job["id"])
            with manager.lock:
                path = manager.media_path(recording_id, "original")
                item = manager.catalog[recording_id]
                source = {"id": recording_id, "camera_id": item["camera_id"],
                          "source_name": item["source_name"], "started_at": item["started_at"],
                          "original_url": f"/api/recordings/{recording_id}/original"}
                manager.analysis_pins.add(recording_id)
            self.recordings = manager
            try:
                return self.create([(f"passage-{recording_id}.mp4", path)],
                    {**options, "task": "full_pipeline", "enable_tracking": True, "max_frames": None}, source_recording=source)
            finally:
                with manager.lock:
                    manager.analysis_pins.discard(recording_id)

    def get(self, task_id: str) -> dict:
        with self._lock:
            if task_id not in self.jobs:
                raise KeyError(task_id)
            return json.loads(json.dumps(self.jobs[task_id]))

    def list(self) -> list[dict]:
        return [self.get(job["id"]) for job in sorted(self.jobs.values(), key=lambda item: item["created_at"], reverse=True)]

    def cancel(self, task_id: str) -> dict:
        with self._lock:
            job = self.jobs[task_id]
            if job["status"] in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
                self._cancel[task_id].set()
                if job["status"] == JobStatus.QUEUED.value:
                    job.update(status=JobStatus.CANCELLED.value, completed_at=utc_now())
                    self._write(job)
            return self.get(task_id)

    def ensure_reconfigurable(self) -> None:
        if any(job["status"] in {JobStatus.QUEUED.value, JobStatus.RUNNING.value} for job in self.jobs.values()):
            raise RuntimeError("存在排队或运行任务，不能重载模型")

    def reconfigure(self, settings: Settings) -> None:
        self.ensure_reconfigurable()
        self.settings = settings
        if self._models:
            self._models.release()
            self._models = None

    def _consume(self) -> None:
        while True:
            task_id = self._queue.get()
            if task_id is None:
                return
            if self.jobs.get(task_id, {}).get("status") != JobStatus.CANCELLED.value:
                self._run(task_id)

    def _pipeline(self, storage: ResultStorage) -> Pipeline:
        if self._models is None:
            self._models = ModelRegistry(self.settings)
            try:
                with inference_scheduler.slot():
                    self._models.load()
            except Exception:
                self._models.release()
                self._models = None
                raise
        return Pipeline(
            FrameInferenceService(self._models),
            storage,
            ResultRepository(SessionLocal),
            tracker_factory=lambda: TrackTrackTracker(det_thresh=.45, init_thresh=.45, match_thresh=.8, max_time_lost=30, min_len=1, min_box_area=800, class_agnostic=True),
            tracking_options={
                "confirm_hits": self.settings.track_confirm_hits,
                "lost_seconds": self.settings.track_lost_seconds,
                "finish_seconds": self.settings.track_finish_seconds,
            },
        )

    def _run(self, task_id: str) -> None:
        job = self.jobs[task_id]
        try:
            if self._cancel[task_id].is_set():
                raise InterruptedError()
            job.update(status=JobStatus.RUNNING.value, started_at=utc_now())
            self._write(job)
            storage = ResultStorage.open(self.settings.result_root, task_id)
            pipeline = self._pipeline(storage)
            rows: list[dict] = []
            completed = failed = 0
            for ordinal, item in enumerate(job["items"], 1):
                if self._cancel[task_id].is_set():
                    raise InterruptedError()
                source = self.settings.upload_root / task_id / item["upload_name"]
                try:
                    def report(processed, message):
                        item.update(processed_samples=processed, message=message, status="running")
                        if processed == 1 or processed % 20 == 0:
                            self._write(job)
                    outcome = pipeline.process_file(
                        source,
                        ordinal=ordinal,
                        source_filename=item["filename"],
                        task=TaskKind(job["options"]["task"]),
                        enable_tracking=job["options"]["enable_tracking"],
                        frame_step=job["options"]["frame_step"],
                        max_frames=job["options"]["max_frames"],
                        visualize=job["options"]["visualize"],
                        cancelled=self._cancel[task_id].is_set,
                        progress=report,
                        **({"observed_start": datetime.fromisoformat(job["source_recording"]["started_at"])} if job.get("source_recording") else {}),
                    )
                except InterruptedError:
                    item.update(status="cancelled")
                    raise
                except Exception as exc:
                    item.update(status="failed", progress=100, error=str(exc))
                    failed += 1
                else:
                    item.update(status="completed", progress=100, result_uri=outcome["result_uri"], visual_uri=outcome["visual_uri"])
                    rows.extend(outcome["csv_rows"])
                    item["processed_samples"] = outcome.get("processed_samples", 0)
                    completed += 1
                job["progress"] = round(100 * ordinal / len(job["items"]))
                self._write(job)
            self._write_result_csv(storage, rows, job)
            if job["options"]["enable_tracking"]:
                job["summary"] = {"instances": rows, "ship_names": sorted({row["recognized_zh"] for row in rows if row.get("recognized_zh") not in {None, "", "UNKNOWN"}}),
                                  "draft_depths_m": [row["draft_depth_m"] for row in rows if row.get("draft_depth_m") is not None]}
            if completed:
                job.update(status=JobStatus.COMPLETED.value, completed_at=utc_now(), progress=100, error=(f"{failed} file(s) failed" if failed else None))
            else:
                job.update(status=JobStatus.FAILED.value, completed_at=utc_now(), error="; ".join(item["error"] for item in job["items"] if item.get("error")) or "all files failed")
        except InterruptedError:
            job.update(status=JobStatus.CANCELLED.value, completed_at=utc_now())
        except Exception as exc:
            job.update(status=JobStatus.FAILED.value, error=str(exc), completed_at=utc_now())
        finally:
            self._write(job)

    @staticmethod
    def _write_result_csv(storage: ResultStorage, rows: list[dict], job: dict) -> None:
        tracking = bool(job["options"]["enable_tracking"])
        sources = [Path(item["filename"]).suffix.lower() for item in job["items"]]
        video_untracked = not tracking and bool(sources) and all(suffix in VIDEO_EXTENSIONS for suffix in sources)
        storage.write_csv(rows, TaskKind(job["options"]["task"]), tracking=tracking, video_untracked=video_untracked)

    def _write(self, job: dict) -> None:
        path = self.settings.result_root / job["id"] / "job.json"
        from .recording import _atomic_json
        _atomic_json(path, job)
        self._sync_recording(job)

    def _sync_recording(self, job):
        if self.recordings and job.get("source_recording"):
            self.recordings.update_analysis(job)

    def _load(self) -> None:
        for path in self.settings.result_root.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job.get("status") in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
                    job.update(status=JobStatus.FAILED.value, error="service restarted before completion", completed_at=utc_now())
                self.jobs[job["id"]] = job
                self._cancel[job["id"]] = threading.Event()
                self._write(job)
            except Exception:
                continue


job_manager = JobManager()
