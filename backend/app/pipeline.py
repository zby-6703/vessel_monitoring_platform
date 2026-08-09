"""The only batch/video orchestration implementation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from .schemas import FrameResultV2, TaskKind
from .inference.frame_inference import FrameInferenceService
from .database import ResultRepository
from .storage import ResultStorage
from .tracking import InstanceAggregator

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".flv", ".ts"}


@dataclass(frozen=True)
class VideoSample:
    frame_index: int
    source_frame_index: int
    observed_at: datetime
    frame: np.ndarray


def sample_video(path: Path, frame_step: int = 1, max_frames: int | None = None):
    """Stream video frames; never load a video or its JSONL into memory."""
    if frame_step < 1 or (max_frames is not None and max_frames < 1): raise ValueError("frame_step and max_frames must be positive")
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened(): raise ValueError(f"unable to read video: {path}")
    fps, source_index, frame_index = float(capture.get(cv2.CAP_PROP_FPS) or 25.0), -1, 0
    base_time = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    try:
        while True:
            ok, frame = capture.read()
            if not ok: return
            source_index += 1
            if source_index % frame_step: continue
            yield VideoSample(frame_index, source_index, base_time + timedelta(seconds=source_index / fps), frame)
            frame_index += 1
            if max_frames is not None and frame_index >= max_frames: return
    finally:
        capture.release()


class Pipeline:
    def __init__(self, frame_inference: FrameInferenceService, storage: ResultStorage, repository: ResultRepository | None = None, tracker_factory: Callable[[], Any] | None = None, tracking_options: dict[str, float | int] | None = None) -> None:
        self.frame_inference = frame_inference
        self.storage = storage
        self.repository = repository
        self.tracker_factory = tracker_factory
        self.tracking_options = tracking_options or {}

    def process_file(self, source: Path, *, ordinal: int, task: TaskKind, enable_tracking: bool, source_filename: str | None = None, frame_step: int = 1, max_frames: int | None = None, visualize: bool = False, progress: Callable[[int, str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict:
        source_filename = source_filename or source.name
        suffix = source.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return self._image(source, source_filename, ordinal, task, visualize, progress, cancelled)
        if suffix in VIDEO_EXTENSIONS:
            return self._video(source, source_filename, ordinal, task, enable_tracking, frame_step, max_frames, visualize, progress, cancelled)
        raise ValueError(f"unsupported media type: {source.name}")

    def _image(self, source: Path, source_filename: str, ordinal: int, task: TaskKind, visualize: bool, progress, cancelled) -> dict:
        self._check_cancelled(cancelled)
        frame = cv2.imread(str(source))
        if frame is None:
            raise ValueError(f"unable to read image: {source.name}")
        result = self.frame_inference.infer(frame, task_id=self.storage.task_id, source_filename=source_filename, task=task, observed_at=datetime.now(timezone.utc))
        stem = self.storage.source_stem(ordinal, source_filename)
        label_uri = None
        if task is not TaskKind.SHIP_NAME_RECOGNITION:
            label_uri = self.storage.relative_uri(self.storage.write_image_label(stem, result, frame))
        visual_uri = None
        if visualize:
            visual_uri = self.storage.relative_uri(self.storage.write_image_visualization(stem, self._visualize(frame, result)))
        if task is TaskKind.FULL_PIPELINE and self.repository and label_uri:
            self.repository.save_image_frame(result, label_uri)
        self._notify(progress, 1, "image completed")
        return {"filename": source_filename, "mode": "image", "status": "completed", "processed_samples": 1, "csv_rows": self._image_rows(result, task), "result_uri": label_uri, "visual_uri": visual_uri}

    def _video(self, source: Path, source_filename: str, ordinal: int, task: TaskKind, enable_tracking: bool, frame_step: int, max_frames: int | None, visualize: bool, progress, cancelled) -> dict:
        stem = self.storage.source_stem(ordinal, source_filename)
        tracker = self.tracker_factory() if enable_tracking and self.tracker_factory else None
        aggregator = InstanceAggregator(self.storage.task_id, source_filename, **self.tracking_options) if enable_tracking else None
        rows: list[dict] = []
        processed = 0
        jsonl_uri: str | None = None
        last_time = datetime.now(timezone.utc)
        writer = None
        try:
            for sample in sample_video(source, frame_step, max_frames):
                self._check_cancelled(cancelled)
                result = self.frame_inference.infer(sample.frame, task_id=self.storage.task_id, source_filename=source_filename, task=task, observed_at=sample.observed_at, frame_index=sample.frame_index, source_frame_index=sample.source_frame_index)
                last_time = sample.observed_at
                active_ids: set[int] = set()
                if tracker is not None:
                    # TrackTrack rejects zero and repeated IDs; frame_index is
                    # a public zero-based ordinal, so only the tracker receives
                    # its one-based counterpart.
                    tracks = self._tracks_for_frame(tracker, result, sample.frame, sample.frame_index + 1)
                    for vessel in result.vessels:
                        match = self._best_track(vessel.ship.xyxy, tracks)
                        if match is not None:
                            vessel.track_id = int(match.track_id)
                            active_ids.add(vessel.track_id)
                            aggregator.update(vessel.track_id, vessel, sample.observed_at)
                    for instance in aggregator.sweep(active_ids, sample.observed_at):
                        self.storage.append_video_record(stem, instance)
                jsonl_uri = self.storage.relative_uri(self.storage.append_video_record(stem, result))
                if visualize:
                    canvas = self._visualize(sample.frame, result)
                    if writer is None:
                        height, width = canvas.shape[:2]
                        writer = cv2.VideoWriter(str(self.storage.video_visualization_path(stem)), cv2.VideoWriter_fourcc(*"mp4v"), 25.0 / frame_step, (width, height))
                        if not writer.isOpened(): raise RuntimeError("unable to create annotated video")
                    writer.write(canvas)
                if not enable_tracking:
                    rows.extend({"frame_index": result.frame_index, "recognized_zh": vessel.recognized_zh, "recognized_en": vessel.recognized_en, "draft_depth_m": vessel.draft.depth_m} for vessel in result.vessels)
                    if task is TaskKind.FULL_PIPELINE and self.repository and jsonl_uri:
                        self.repository.save_image_frame(result, jsonl_uri)
                processed += 1
                self._notify(progress, processed, f"processed video frame {sample.source_frame_index}")
        finally:
            if writer is not None: writer.release()
        if aggregator is not None:
            for instance in aggregator.finish_all(last_time):
                self.storage.append_video_record(stem, instance)
                rows.append({"instance_id": instance.instance_id, "recognized_zh": instance.recognized_zh, "recognized_en": instance.recognized_en, "draft_depth_m": instance.draft_depth_m, "start_time": instance.start_time.isoformat(), "end_time": instance.end_time.isoformat(), "status": instance.status.value})
                if task is TaskKind.FULL_PIPELINE and self.repository and jsonl_uri:
                    self.repository.save_video_instance(instance, jsonl_uri)
        if visualize and self.storage.video_visualization_path(stem).is_file():
            self.storage.make_video_visualization_web_playable(stem)
        visual_uri = self.storage.relative_uri(self.storage.video_visualization_path(stem)) if visualize and self.storage.video_visualization_path(stem).is_file() else None
        return {"filename": source_filename, "mode": "video", "status": "completed", "processed_samples": processed, "csv_rows": rows, "result_uri": jsonl_uri, "visual_uri": visual_uri, "enable_tracking": enable_tracking}

    @staticmethod
    def _image_rows(result: FrameResultV2, task: TaskKind) -> list[dict]:
        return [{"filename": result.source_filename, "recognized_zh": vessel.recognized_zh, "recognized_en": vessel.recognized_en, "draft_depth_m": vessel.draft.depth_m} for vessel in result.vessels]

    @staticmethod
    def _tracks_for_frame(tracker: Any, result: FrameResultV2, frame: np.ndarray, frame_index: int):
        from .tracking import detections_from_arrays
        boxes = np.asarray([vessel.ship.xyxy for vessel in result.vessels], dtype=np.float32).reshape(-1, 4)
        scores = np.asarray([vessel.ship.confidence for vessel in result.vessels], dtype=np.float32)
        return tracker.update(detections_from_arrays(boxes, scores, np.zeros(len(boxes), dtype=np.int64), class_names=["ship"]), frame_id=frame_index, image=frame)

    @staticmethod
    def _best_track(box: tuple[float, float, float, float], tracks: list[Any]) -> Any | None:
        best = (0.0, None)
        for track in tracks:
            x1, y1, x2, y2 = box
            tx1, ty1, tx2, ty2 = [float(value) for value in track.tlbr]
            overlap = max(0, min(x2, tx2) - max(x1, tx1)) * max(0, min(y2, ty2) - max(y1, ty1))
            union = (x2 - x1) * (y2 - y1) + (tx2 - tx1) * (ty2 - ty1) - overlap
            iou = overlap / union if union else 0.0
            if iou > best[0]:
                best = (iou, track)
        return best[1]

    @staticmethod
    def _visualize(frame: np.ndarray, result: FrameResultV2) -> np.ndarray:
        canvas = frame.copy()
        colors = {"ship": (199, 183, 39), "ship_name_area": (138, 189, 97), "waterline_area": (66, 136, 232), "draft_mark": (63, 189, 240)}
        for vessel in result.vessels:
            overlay = canvas.copy()
            for water in vessel.water:
                points = np.asarray(water.points, dtype=np.int32).reshape(-1, 1, 2)
                cv2.fillPoly(overlay, [points], (204, 118, 42))
            cv2.addWeighted(overlay, .28, canvas, .72, 0, canvas)
            for detection in [vessel.ship, *vessel.regions, *vessel.draft_marks]:
                x1, y1, x2, y2 = (int(round(value)) for value in detection.xyxy)
                color = colors[detection.label]
                thickness = 2 if detection.label == "ship" else 1
                cv2.rectangle(canvas, (x1, y1), (x2, y2), color, thickness, cv2.LINE_AA)
                if detection.label == "ship":
                    cv2.putText(canvas, f"ID {vessel.track_id or vessel.vessel_id}", (max(2, x1), max(14, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX, .42, color, 1, cv2.LINE_AA)
                elif detection.label == "draft_mark" and detection.class_name:
                    cv2.putText(canvas, str(detection.class_name), (max(2, x1), max(14, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX, .42, color, 1, cv2.LINE_AA)
            readings = []
            if vessel.recognized_zh != "UNKNOWN": readings.append(vessel.recognized_zh)
            if vessel.draft.depth_m is not None: readings.append(f"{vessel.draft.depth_m:.2f} m")
            status = "  |  ".join(readings)
            cv2.rectangle(canvas, (0, max(0, canvas.shape[0] - 30)), (canvas.shape[1] - 1, canvas.shape[0] - 1), (8, 12, 14), -1)
            if status:
                cv2.putText(canvas, status, (10, canvas.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, .5, (66, 136, 232), 1, cv2.LINE_AA)
        return canvas

    @staticmethod
    def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
        if cancelled and cancelled():
            raise InterruptedError("job cancelled")

    @staticmethod
    def _notify(callback: Callable[[int, str], None] | None, current: int, message: str) -> None:
        if callback:
            callback(current, message)
