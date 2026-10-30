"""Compare full recognition with selective live recognition on identical video frames.

Run with the Train interpreter. Only ignored benchmark artifacts are written;
no camera connection, production database or recording is started.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CountedModel:
    def __init__(self, model):
        self.model = model
        self.calls = 0
        self.elapsed = 0.

    def __getattr__(self, name):
        return getattr(self.model, name)

    def _run(self, method, *args, **kwargs):
        self.calls += 1
        started = time.perf_counter()
        try:
            return method(*args, **kwargs)
        finally:
            self.elapsed += time.perf_counter() - started

    def __call__(self, *args, **kwargs):
        return self._run(self.model, *args, **kwargs)

    def predict(self, *args, **kwargs):
        return self._run(self.model.predict, *args, **kwargs)

    def reset(self):
        self.calls, self.elapsed = 0, 0.


def metadata(path):
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError("Cannot open the supplied local video")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not math.isfinite(fps) or fps <= 0 or count <= 0:
            raise ValueError("Video must expose valid frame count and FPS for uniform sampling")
        return {"filename": path.name, "bytes": path.stat().st_size, "fps": fps,
                "frame_count": count, "duration_seconds": count / fps,
                "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))}
    finally:
        capture.release()


def samples(path, indices, fps):
    """Decode sequentially; at most one source image is retained by the iterator."""
    capture = cv2.VideoCapture(str(path))
    wanted = set(indices)
    index = 0
    previous = None
    try:
        if not capture.isOpened():
            raise ValueError("Cannot open the supplied local video")
        while index <= indices[-1]:
            ok, image = capture.read()
            if not ok:
                raise ValueError(f"Video ended before sampled frame {indices[-1]}")
            if index in wanted:
                pts = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000
                if not math.isfinite(pts) or pts < 0 or previous is not None and pts <= previous:
                    pts = index / fps
                    clock = "frame_index/fps"
                else:
                    clock = "decoded_pts"
                previous = pts
                yield index, pts, clock, image
            index += 1
    finally:
        capture.release()


def preview(image, result, index, pts, path):
    canvas = image.copy()
    scale = min(1., 1600 / canvas.shape[1])
    canvas = cv2.resize(canvas, None, fx=scale, fy=scale)
    for vessel in result.vessels:
        decision = vessel.processing
        eligible = bool(decision.get("eligible"))
        color = (80, 205, 80) if eligible else (170, 170, 170)
        x1, y1, x2, y2 = (round(value * scale) for value in vessel.ship.xyxy)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)
        label = f"ID {vessel.track_id or vessel.vessel_id}: {decision.get('reason', 'unknown')}"
        cv2.putText(canvas, label, (max(5, x1), max(65, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1, cv2.LINE_AA)
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 53), (20, 25, 30), -1)
    summary = result.processing_summary
    text = (f"Selective inference benchmark | frame {index} | {pts:.2f}s | "
            f"detected {summary.get('detected', 0)} / eligible {summary.get('eligible', 0)} / detail {summary.get('detail_inferred', 0)}")
    cv2.putText(canvas, text, (10, 32), cv2.FONT_HERSHEY_SIMPLEX, .6, (240, 240, 240), 1, cv2.LINE_AA)
    if not cv2.imwrite(str(path), canvas):
        raise OSError("Unable to write benchmark preview")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, default=ROOT / "assets/20251211082541.mp4")
    parser.add_argument("--samples", type=int, default=120)
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    parser.add_argument("--output", type=Path, default=ROOT / "data/model_validation" / today)
    args = parser.parse_args()
    if not 1 <= args.samples <= 120:
        parser.error("--samples must be between 1 and 120")
    source = args.video.resolve()
    output = args.output.resolve()
    ignored_root = (ROOT / "data/model_validation").resolve()
    if not output.is_relative_to(ignored_root):
        parser.error("--output must be inside ignored data/model_validation")
    output.mkdir(parents=True, exist_ok=True)

    # Imports may instantiate service singletons. Give those an isolated result
    # directory and an in-memory database before loading any application module.
    os.environ["RESULT_ROOT"] = str(output / "target-filter-isolated-runtime")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    for name in ("LIVE_CAPTURE_URL", "CAMERA_ONVIF_DEVICE_URL", "CAMERA_ONVIF_PTZ_URL",
                 "CAMERA_ONVIF_IMAGING_URL", "CAMERA_ONVIF_USERNAME", "CAMERA_ONVIF_PASSWORD"):
        os.environ[name] = ""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.config import get_effective_settings
    from app.inference.frame_inference import FrameInferenceService
    from app.inference.model_registry import ModelRegistry
    from app.pipeline import Pipeline
    from app.recording import RecordingSettings, recording_manager
    from app.schemas import TaskKind
    from app.target_processing import RealtimeTargetProcessor
    from app.tracking import TrackTrackTracker

    info = metadata(source)
    info["sha256"] = file_sha256(source)
    indices = sorted(set(np.linspace(0, info["frame_count"] - 1,
                                     min(args.samples, info["frame_count"]), dtype=int).tolist()))
    settings = get_effective_settings()
    if not settings.device.lower().startswith(("cuda", "gpu")):
        raise RuntimeError("This comparison requires the configured CUDA deployment")
    policy = RecordingSettings()
    models = ModelRegistry(settings)
    try:
        load_started = time.perf_counter()
        models.load()
        load_seconds = time.perf_counter() - load_started
        providers = {name: getattr(models, name).session.get_providers()
                     for name in ("ship_detector", "name_recognizer", "draft_predictor")}
        for name in providers:
            setattr(models, name, CountedModel(getattr(models, name)))
        service = FrameInferenceService(models)
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Two actual decoded frames prewarm the complete model route. Warm-up
        # calls, file decoding, hashes, drawing and JSON I/O are outside timing.
        for ordinal, (index, pts, _clock, image) in enumerate(samples(source, indices[:2], info["fps"])):
            result = service.infer(image, task_id="benchmark-warmup", source_filename=source.name,
                task=TaskKind.FULL_PIPELINE, observed_at=base_time + timedelta(seconds=pts),
                frame_index=ordinal, source_frame_index=index)
            if result.errors:
                raise RuntimeError("Model errors during benchmark warm-up")
        report = {"created_at": datetime.now(timezone.utc).isoformat(), "video": info,
                  "device": settings.device, "providers": providers,
                  "model_load_seconds": load_seconds, "warmup_frames": min(2, len(indices)),
                  "policy": policy.model_dump(), "sample_indices": indices,
                  "timing_scope": "detector + real TrackTrack + downstream recognition or admission gate; excludes decode, hashes, render, disk/database/recording, model loading and warmup",
                  "models": {name: {"package": getattr(settings, config_name).parent.name,
                      "weights_sha256": file_sha256(getattr(settings, weight_name))}
                      for name, config_name, weight_name in (
                          ("ship_detector", "ship_detector_config", "ship_detector_weights"),
                          ("name_recognizer", "shipname_config", "shipname_weights"),
                          ("draft_predictor", "draftformer_config", "draftformer_weights"))},
                  "runtime": {"python": sys.version.split()[0], "opencv": cv2.__version__, "numpy": np.__version__},
                  "source_decode_errors": [],
                  "notes": ["Uniform source-frame sampling over the entire clip; source PTS preferred.",
                            "The supplied clip visibly carries camera OSD Z:3. Offline admission treats this recorded field of view as its reference 1x plane; no absolute camera magnification is inferred or calibrated.",
                            "Two decoded frames execute the full route for warmup; individual OCR/draft model warmup depends on detected regions, so initial actual calls may still include initialization cost.",
                            "Apparent hull size is a scene-specific proxy, not an actual vessel class.",
                            "Throughput is one sampled offline comparison, not a guaranteed live-camera FPS or recognition accuracy result.",
                            "No camera, production database or production recordings are accessed."], "runs": {}}
        for mode in ("full_pipeline", "selective"):
            for name in providers:
                getattr(models, name).reset()
            tracker = TrackTrackTracker(det_thresh=.45, init_thresh=.45, match_thresh=.8,
                max_time_lost=30, min_len=1, min_box_area=800, class_agnostic=True)
            processor = RealtimeTargetProcessor()
            rows, reasons, errors = [], Counter(), Counter()
            best_preview = (-1, -1, -1)
            for ordinal, (index, pts, clock, image) in enumerate(samples(source, indices, info["fps"])):
                digest = hashlib.sha256(memoryview(image)).hexdigest()
                started = time.perf_counter()
                result = service.infer(image, task_id=f"benchmark-{mode}", source_filename=source.name,
                    task=TaskKind.FULL_PIPELINE if mode == "full_pipeline" else TaskKind.REGION_DETECTION,
                    observed_at=base_time + timedelta(seconds=pts), frame_index=ordinal, source_frame_index=index)
                inferred = time.perf_counter()
                tracks = Pipeline._tracks_for_frame(tracker, result, image, ordinal + 1)
                for vessel in result.vessels:
                    match = Pipeline._best_track(vessel.ship.xyxy, tracks)
                    if match is not None:
                        vessel.track_id = int(match.track_id)
                tracked = time.perf_counter()
                if mode == "selective":
                    processor.process(service, result, image, pts, policy,
                        {"zoom_factor": 1., "accept_detections": True, "generation": 0})
                ended = time.perf_counter()
                summary = result.processing_summary if mode == "selective" else {
                    "detected": len(result.vessels), "eligible": len(result.vessels),
                    "detail_inferred": len(result.vessels), "lightweight": 0, "detail_skipped": 0}
                frame_reasons = Counter(str(v.processing.get("reason", "full_pipeline")) for v in result.vessels)
                reasons.update(frame_reasons)
                errors.update(error.split(":", 1)[0] for error in result.errors)
                rows.append({"source_frame_index": index, "timestamp_seconds": pts, "clock": clock,
                    "sha256": digest, **summary, "reasons": dict(frame_reasons),
                    "inference_ms": (inferred - started) * 1000, "tracking_ms": (tracked - inferred) * 1000,
                    "gate_and_detail_ms": (ended - tracked) * 1000, "total_ms": (ended - started) * 1000})
                if mode == "selective":
                    rank = (summary.get("detail_inferred", 0), summary.get("eligible", 0), summary.get("detected", 0))
                    if rank > best_preview:
                        preview(image, result, index, pts, output / "target_filter_preview.jpg")
                        best_preview = rank
            elapsed_ms = sum(row["total_ms"] for row in rows)
            report["runs"][mode] = {"frames": len(rows), "processing_seconds": elapsed_ms / 1000,
                "mean_ms": elapsed_ms / len(rows), "p50_ms": float(np.percentile([row["total_ms"] for row in rows], 50)),
                "p95_ms": float(np.percentile([row["total_ms"] for row in rows], 95)),
                "throughput_fps": len(rows) * 1000 / elapsed_ms,
                "totals": {key: sum(row[key] for row in rows) for key in ("detected", "eligible", "detail_inferred", "lightweight", "detail_skipped")},
                "reasons": dict(reasons), "model_errors": dict(errors),
                "model_calls": {name: {"calls": getattr(models, name).calls,
                    "elapsed_ms": getattr(models, name).elapsed * 1000} for name in providers}, "samples": rows}
            print(json.dumps({"mode": mode, **{key: report["runs"][mode][key]
                for key in ("frames", "mean_ms", "throughput_fps", "totals", "model_calls", "model_errors")}}, ensure_ascii=False), flush=True)
        baseline, selective = (report["runs"][mode] for mode in ("full_pipeline", "selective"))
        report["identical_decoded_samples"] = all(
            a["source_frame_index"] == b["source_frame_index"] and a["sha256"] == b["sha256"]
            for a, b in zip(baseline["samples"], selective["samples"])) and baseline["frames"] == selective["frames"]
        if not report["identical_decoded_samples"]:
            raise RuntimeError("The two video decodes did not produce identical sampled frames")
        report["comparison"] = {
            "processing_time_reduction_percent": (1 - selective["processing_seconds"] / baseline["processing_seconds"]) * 100,
            "name_calls_saved": baseline["model_calls"]["name_recognizer"]["calls"] - selective["model_calls"]["name_recognizer"]["calls"],
            "draft_calls_saved": baseline["model_calls"]["draft_predictor"]["calls"] - selective["model_calls"]["draft_predictor"]["calls"],
            "eligible_observations_with_default_policy": selective["totals"]["eligible"],
        }
        destination = output / "target_filter_benchmark.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(destination), "identical_decoded_samples": True, **report["comparison"]}, ensure_ascii=False))
    finally:
        models.release()
        recording_manager.control_executor.shutdown(wait=True)
        recording_manager.executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
