"""Flat, side-effect-only output writer for pipeline results."""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

from .schemas import FrameResultV2, InstanceResultV2, TaskKind


def _safe_stem(filename: str) -> str:
    value = Path(filename).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "source"


class ResultStorage:
    """Only this class writes JSON/JSONL/CSV/JPG/MP4 result files."""

    def __init__(self, result_root: Path, task_id: str) -> None:
        self.result_root = result_root.resolve()
        self.task_id = task_id
        self.directory = self.result_root / task_id
        self.directory.mkdir(parents=True, exist_ok=False)

    @classmethod
    def open(cls, result_root: Path, task_id: str) -> "ResultStorage":
        instance = cls.__new__(cls)
        instance.result_root = result_root.resolve()
        instance.task_id = task_id
        instance.directory = instance.result_root / task_id
        if not instance.directory.is_dir():
            raise FileNotFoundError(instance.directory)
        return instance

    def relative_uri(self, path: Path) -> str:
        return path.resolve().relative_to(self.result_root).as_posix()

    def source_stem(self, ordinal: int, source_filename: str) -> str:
        return f"{ordinal:03d}_{_safe_stem(source_filename)}"

    def write_job(self, job: dict) -> Path:
        return self._atomic_json(self.directory / "job.json", job)

    def write_image_label(self, stem: str, frame: FrameResultV2, image: np.ndarray) -> Path:
        document = {
            "version": "2.0",
            "flags": {},
            "imagePath": frame.source_filename,
            "imageData": None,
            "imageWidth": frame.image_width,
            "imageHeight": frame.image_height,
            "shapes": self._labelme_shapes(frame),
            "results": [
                {
                    "recognized_zh": vessel.recognized_zh,
                    "recognized_en": vessel.recognized_en,
                    "draft_depth_m": vessel.draft.depth_m,
                    "draft_success": vessel.draft.success,
                    "draft_method": vessel.draft.method,
                    "errors": vessel.errors,
                }
                for vessel in frame.vessels
            ],
            "frame": frame.model_dump(mode="json"),
        }
        return self._atomic_json(self.directory / f"{stem}.json", document)

    def append_video_record(self, stem: str, record: FrameResultV2 | InstanceResultV2) -> Path:
        path = self.directory / f"{stem}.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.model_dump_json() + "\n")
        return path

    def write_csv(self, rows: list[dict[str, object]], task: TaskKind, tracking: bool = False, video_untracked: bool = False) -> Path:
        if tracking:
            fields = ["instance_id", "recognized_zh", "recognized_en", "draft_depth_m", "start_time", "end_time", "status"]
        elif video_untracked:
            fields = ["frame_index", "recognized_zh", "recognized_en", "draft_depth_m"]
        elif task is TaskKind.SHIP_NAME_RECOGNITION:
            fields = ["filename", "recognized_zh", "recognized_en"]
        elif task is TaskKind.DRAFT_ESTIMATION:
            fields = ["filename", "draft_depth_m"]
        elif task is TaskKind.FULL_PIPELINE:
            fields = ["filename", "recognized_zh", "recognized_en", "draft_depth_m"]
        else:
            fields = ["filename"]
        path = self.directory / "result.csv"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fields})
        os.replace(temporary, path)
        return path

    def write_image_visualization(self, stem: str, frame: np.ndarray) -> Path:
        path = self.directory / f"{stem}_vis.jpg"
        if not cv2.imwrite(str(path), frame):
            raise RuntimeError(f"unable to write visualization {path}")
        return path

    def video_visualization_path(self, stem: str) -> Path:
        return self.directory / f"{stem}_annotated.mp4"

    def make_video_visualization_web_playable(self, stem: str) -> Path:
        """Replace OpenCV's MPEG-4 Part 2 output with browser-safe H.264 MP4."""
        source = self.video_visualization_path(stem)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to create browser-playable annotated video")
        temporary = source.with_name(f".{source.stem}.h264.mp4")
        completed = subprocess.run(
            [ffmpeg, "-y", "-i", str(source), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode or not temporary.is_file():
            raise RuntimeError(f"unable to encode browser-playable video: {completed.stderr[-400:]}")
        os.replace(temporary, source)
        return source

    @staticmethod
    def _labelme_shapes(frame: FrameResultV2) -> list[dict]:
        shapes = []
        for vessel in frame.vessels:
            for detection in [vessel.ship, *vessel.regions, *vessel.draft_marks]:
                x1, y1, x2, y2 = detection.xyxy
                label = detection.class_name or detection.label
                shapes.append({"label": label, "shape_type": "rectangle", "points": [[x1, y1], [x2, y2]], "confidence": detection.confidence})
            for polygon in vessel.water:
                shapes.append({"label": "water", "shape_type": "polygon", "points": polygon.points, "confidence": polygon.confidence})
        return shapes

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> Path:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return path
