"""Replay real detections through vessel selection without controlling a camera.

Example (run with the project's Train interpreter):
    python backend/scripts/simulate_adaptive_monitor.py --input assets/example.mp4 \
        --output data/adaptive-example --sample-fps 4

The MP4 is a sampled, annotated strategy replay of the ORIGINAL wide view.
It does not simulate optical image quality or send any physical lens commands.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sample-fps", type=float, default=4)
    parser.add_argument("--detect-only", action="store_true")
    parser.add_argument("--reuse-detections", action="store_true")
    parser.add_argument("--settings", type=Path, help="Optional RecordingSettings JSON overrides")
    parser.add_argument("--calibrated-factors", default="1,2,3,4,6,8", help="Simulated available lens magnifications")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error("input video does not exist")
    if not .1 <= args.sample_fps <= 20:
        parser.error("sample FPS must be between 0.1 and 20")
    return args


def video_info(path):
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise RuntimeError("cannot decode input video")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not math.isfinite(fps) or fps <= 0 or frames <= 0:
            raise RuntimeError("input does not report a valid FPS/frame count")
        return {"width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "fps": fps, "source_frames": frames, "duration_seconds": frames / fps}
    finally:
        capture.release()


def contact_sheet(source, output, info):
    capture = cv2.VideoCapture(str(source))
    tiles = []
    try:
        for index, timestamp in enumerate(np.linspace(0, max(0, info["duration_seconds"] - .15), 9)):
            capture.set(cv2.CAP_PROP_POS_MSEC, float(timestamp) * 1000)
            ok, image = capture.read()
            if not ok:
                continue
            tile = cv2.resize(image, (640, 360))
            cv2.rectangle(tile, (0, 0), (640, 34), (12, 20, 27), -1)
            cv2.putText(tile, f"{timestamp:.2f}s", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, .7, (230, 235, 240), 2)
            cv2.imwrite(str(output / f"scene-{index:02d}.jpg"), image)
            tiles.append(tile)
        while len(tiles) < 9:
            tiles.append(np.zeros((360, 640, 3), dtype=np.uint8))
        cv2.imwrite(str(output / "contactsheet.jpg"), np.vstack([np.hstack(tiles[i:i + 3]) for i in range(0, 9, 3)]))
    finally:
        capture.release()


def live_vessels(result):
    values = []
    for vessel in result.vessels:
        boxes = []
        for box in [vessel.ship, *vessel.regions]:
            x1, y1, x2, y2 = box.xyxy
            boxes.append({"type": box.label, "xywh": [x1, y1, x2 - x1, y2 - y1], "confidence": box.confidence})
        values.append({"ship_id": vessel.vessel_id, "ship_name": "UNKNOWN", "confidence": vessel.ship.confidence, "boxes": boxes})
    return values


def cached_frames(source, samples):
    """Decode a sorted cache in one pass instead of repeatedly seeking a GOP."""
    capture = cv2.VideoCapture(str(source))
    current = -1
    try:
        if not capture.isOpened():
            raise RuntimeError("cannot decode source video")
        for sample in samples:
            target = int(sample["source_frame_index"])
            if target < current:
                capture.set(cv2.CAP_PROP_POS_FRAMES, target)
                current = target - 1
            while current < target:
                if not capture.grab():
                    raise RuntimeError("source ended before a cached frame")
                current += 1
            ok, image = capture.retrieve()
            if not ok:
                raise RuntimeError("cannot decode a cached source frame")
            yield sample, image
    finally:
        capture.release()


def detect(args, info, cache):
    from app.config import get_effective_settings
    from app.inference.frame_inference import FrameInferenceService
    from app.inference.model_registry import ModelRegistry
    from app.schemas import TaskKind

    settings = get_effective_settings()
    registry = ModelRegistry(settings)
    capture = cv2.VideoCapture(str(args.input))
    started = time.monotonic()
    samples = []
    try:
        registry.load()
        service = FrameInferenceService(registry)
        count = math.ceil(info["duration_seconds"] * args.sample_fps)
        with cache.open("w", encoding="utf-8") as handle:
            for index in range(count):
                timestamp = index / args.sample_fps
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
                ok, image = capture.read()
                if not ok:
                    break
                source_index = max(0, round(capture.get(cv2.CAP_PROP_POS_FRAMES)) - 1)
                decoded_ms = float(capture.get(cv2.CAP_PROP_POS_MSEC))
                media_timestamp = decoded_ms / 1000 if math.isfinite(decoded_ms) and decoded_ms >= 0 else source_index / info["fps"]
                result = service.infer(image, task_id="adaptive-scene-replay", source_filename=args.input.name,
                    task=TaskKind.REGION_DETECTION, observed_at=datetime.now(timezone.utc), frame_index=index, source_frame_index=source_index)
                critical = [e for e in result.errors if str(e).startswith(("ship_detector_failed:", "frame_inference_failed:"))]
                if critical:
                    raise RuntimeError("; ".join(critical))
                item = {"timestamp": media_timestamp, "requested_timestamp": timestamp, "clock": "decoded_pts",
                        "source_frame_index": source_index, "width": image.shape[1], "height": image.shape[0],
                        "vessels": live_vessels(result), "processing_ms": result.processing_ms, "errors": result.errors}
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                handle.flush()
                samples.append(item)
                if index % 20 == 0 or index == count - 1:
                    print(json.dumps({"phase": "detection", "sample": index + 1, "samples": count, "seconds": round(timestamp, 2), "vessels": len(result.vessels)}, ensure_ascii=False), flush=True)
        print(json.dumps({"detection_seconds": round(time.monotonic() - started, 3), "cache": str(cache.resolve())}), flush=True)
    finally:
        capture.release()
        registry.release()
    return samples


def restore_cached_timestamps(args, samples):
    """Upgrade cached frame-index sampling to its actual decoded media clock."""
    if all(sample.get("clock") == "decoded_pts" for sample in samples):
        return False
    capture = cv2.VideoCapture(str(args.input))
    try:
        for sample in samples:
            capture.set(cv2.CAP_PROP_POS_FRAMES, sample["source_frame_index"])
            ok, _ = capture.read()
            if not ok:
                raise RuntimeError("cannot recover the timestamp of a cached source frame")
            decoded_ms = float(capture.get(cv2.CAP_PROP_POS_MSEC))
            if not math.isfinite(decoded_ms) or decoded_ms < 0:
                raise RuntimeError("decoder did not return a usable timestamp for cached source frames")
            sample["requested_timestamp"] = sample["timestamp"]
            sample["timestamp"] = decoded_ms / 1000
            sample["clock"] = "decoded_pts"
    finally:
        capture.release()
    return True


def add_camera_motion(args, samples, cache):
    from app.scene_motion import BackgroundMotionEstimator

    estimator = BackgroundMotionEstimator()
    reader = cached_frames(args.input, samples)
    try:
        for sample, image in reader:
            sample["camera_motion"] = estimator.update(image, sample["vessels"], sample["timestamp"], generation=0)
    finally:
        reader.close()
    with cache.open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")


def annotate(image, decision, timestamp):
    canvas = cv2.resize(image, (960, 540))
    for target in decision.get("targets", []):
        box = target.get("box")
        if not box or len(box) != 4:
            continue
        x1, y1, x2, y2 = [max(0, min(1, float(value))) for value in box]
        selected = target.get("target_id") == decision.get("selected_target_id")
        state = target.get("motion_state", "observing")
        color = (215, 221, 228) if state == "stationary" else (80, 215, 80) if state == "moving" else (50, 195, 235)
        if selected:
            color = (230, 220, 30)
        cv2.rectangle(canvas, (round(x1 * 960), round(y1 * 540)), (round(x2 * 960), round(y2 * 540)), color, 3 if selected else 2)
        label = f"T{target.get('target_id')} {state}" if not selected else f"T{target.get('target_id')} SELECTED h={target.get('height_ratio', 0):.1%}"
        origin = (min(790, round(x1 * 960)), max(46, round(y1 * 540) - 7))
        label_width = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, .48, 1)[0][0]
        cv2.rectangle(canvas, (origin[0] - 1, origin[1] - 16), (min(959, origin[0] + label_width + 3), origin[1] + 3), (12, 20, 27), -1)
        cv2.putText(canvas, label, origin, cv2.FONT_HERSHEY_SIMPLEX, .48, color, 1)
    cv2.rectangle(canvas, (0, 0), (960, 32), (12, 20, 27), -1)
    text = f"STRATEGY REPLAY - NO PHYSICAL ZOOM | t={timestamp:.2f}s | target={decision.get('selected_target_id')} | proposed={decision.get('zoom_factor', 1):.1f}x"
    cv2.putText(canvas, text, (9, 22), cv2.FONT_HERSHEY_SIMPLEX, .49, (230, 235, 240), 1)
    return canvas


def replay(args, info, samples):
    from app.recording import RecordingSettings
    from app.vessel_selection import VesselTargetSelector

    overrides = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings else {}
    policy = RecordingSettings.model_validate(overrides)
    factors = sorted({float(value) for value in args.calibrated_factors.split(",")})
    if not factors or not all(math.isfinite(value) and value >= 1 for value in factors):
        raise ValueError("calibrated factors must be finite magnifications >= 1")
    selector = VesselTargetSelector()
    reader = cached_frames(args.input, samples)
    intermediate = args.output / "strategy.capture.mp4"
    writer = cv2.VideoWriter(str(intermediate), cv2.VideoWriter_fourcc(*"mp4v"), args.sample_fps, (960, 540))
    if not writer.isOpened():
        reader.close()
        raise RuntimeError("cannot create replay MP4")
    decisions = []
    try:
        for sample, image in reader:
            decision = selector.update(sample["vessels"], sample["width"], sample["height"], sample["timestamp"], policy,
                zoom_factor=1., calibrated_factors=factors, camera_motion=sample.get("camera_motion"), image=image)
            record = {"timestamp": sample["timestamp"], **decision}
            decisions.append(record)
            writer.write(annotate(image, decision, sample["timestamp"]))
    finally:
        writer.release()
        reader.close()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for the browser-playable replay")
    destination = args.output / "strategy-replay.mp4"
    subprocess.run([ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-i", str(intermediate), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)], check=True)
    intermediate.unlink()
    with (args.output / "decisions.jsonl").open("w", encoding="utf-8") as handle:
        for decision in decisions:
            handle.write(json.dumps(decision, ensure_ascii=False) + "\n")
    selected = [d for d in decisions if d.get("selected_target_id") is not None]
    states = Counter(t.get("motion_state", "unknown") for d in decisions for t in d.get("targets", []))
    report = {
        "input": str(args.input.resolve()), "source": info, "sample_fps": args.sample_fps, "sample_count": len(samples),
        "detector": "actual configured ship detector; region_detection only", "settings": policy.model_dump(),
        "simulated_calibrated_factors": factors, "physical_camera_controlled": False,
        "selected_observations": len(selected), "selected_target_ids": sorted({d["selected_target_id"] for d in selected}),
        "proposed_zoom_factors": sorted({d.get("zoom_factor", 1) for d in selected}), "motion_state_observations": dict(states),
        "multi_ship_observations": sum(len(s["vessels"]) > 1 for s in samples),
        "stationary_excluded_observations": sum(t.get("motion_state") == "stationary" and not t.get("eligible") for d in decisions for t in d.get("targets", [])),
        "limitations": ["Apparent vessel height is a proximity heuristic, not measured physical distance.",
            "This short clip cannot establish that a vessel remains moored for long periods.",
            "The source watermark reports Z:3 and the pan angle changes; source coordinates are replay-reference coordinates, not proven optical 1x.",
            "The output replays the original wide view; no optical lens or image quality was simulated."],
        "camera_motion_valid_observations": sum(s.get("camera_motion", {}).get("valid", False) for s in samples),
        "accumulated_background_dx": sum(s.get("camera_motion", {}).get("dx", 0) for s in samples if s.get("camera_motion", {}).get("valid")),
        "clock": "decoded_source_pts", "last_sample_source_seconds": samples[-1]["timestamp"] if samples else None,
        "artifacts": {"video": str(destination.resolve()), "contactsheet": str((args.output / "contactsheet.jpg").resolve()),
                      "detections": str((args.output / "detections.jsonl").resolve()), "decisions": str((args.output / "decisions.jsonl").resolve())},
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"settings", "artifacts"}}, ensure_ascii=False), flush=True)


def main():
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    info = video_info(args.input)
    contact_sheet(args.input, args.output, info)
    cache = args.output / "detections.jsonl"
    manifest = args.output / "detection-source.json"
    identity = {"input": str(args.input.resolve()), "bytes": args.input.stat().st_size, "mtime_ns": args.input.stat().st_mtime_ns,
                "sample_fps": args.sample_fps, **info}
    if args.reuse_detections:
        if not cache.is_file() or not manifest.is_file() or json.loads(manifest.read_text(encoding="utf-8")) != identity:
            raise ValueError("cached detections do not match this input/sample rate")
        samples = [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        # A failed rerun must not leave an older "complete" manifest beside a
        # partially replaced detection cache.
        manifest.unlink(missing_ok=True)
        samples = detect(args, info, cache)
        manifest.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    clock_changed = restore_cached_timestamps(args, samples)
    if clock_changed or not all("camera_motion" in sample for sample in samples):
        add_camera_motion(args, samples, cache)
    if not args.detect_only:
        replay(args, info, samples)


if __name__ == "__main__":
    main()
