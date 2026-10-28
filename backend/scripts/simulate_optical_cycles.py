"""Exercise the complete recorder with real cached detections and a fake lens.

The source is a reference wide view. Zoomed images are digital crops and boxes
are geometric projections of its real detections, NOT new model inference or
proof of physical optical magnification/image quality.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import cv2

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.recording import RecordingManager
from scripts.simulate_adaptive_monitor import cached_frames


class FakeCalibratedLens:
    def __init__(self, factors):
        self.factor = 1.
        self.factors = factors
        self.source_seconds = 0.
        self.commands = []
        self.lock = threading.Lock()

    def available_magnifications(self):
        return list(self.factors)

    def validate_cycle(self, factor):
        if factor not in self.factors:
            raise ValueError("simulated magnification is not in the supplied calibration table")

    def move_to(self, factor):
        self.validate_cycle(factor)
        with self.lock:
            self.factor = factor
            self.commands.append({"source_seconds": self.source_seconds, "factor": factor, "settled": True})
        return {"factor": factor, "position": self.factors.index(factor) / max(1, len(self.factors) - 1), "settled": True}

    def cancel_move(self):
        pass


def project(image, vessels, factor, size=(960, 540)):
    height, width = image.shape[:2]
    x1 = .5 - .5 / factor
    y1 = .5 - .5 / factor
    crop = image[round(y1 * height):round((y1 + 1 / factor) * height),
                 round(x1 * width):round((x1 + 1 / factor) * width)]
    view = cv2.resize(crop, size)
    values = []
    for vessel in vessels:
        boxes = []
        for box in vessel.get("boxes", []):
            x, y, w, h = box["xywh"]
            left = max(0., min(1., (x / width - x1) * factor))
            top = max(0., min(1., (y / height - y1) * factor))
            right = max(0., min(1., ((x + w) / width - x1) * factor))
            bottom = max(0., min(1., ((y + h) / height - y1) * factor))
            if right > left and bottom > top:
                boxes.append({**box, "xywh": [left * size[0], top * size[1], (right - left) * size[0], (bottom - top) * size[1]]})
        if any(box["type"] == "ship" for box in boxes):
            values.append({**vessel, "boxes": boxes})
    return view, values


def overlay(image, vessels, status, timestamp, view_factor):
    canvas = image.copy()
    for vessel in vessels:
        ship = next(box for box in vessel["boxes"] if box["type"] == "ship")
        x, y, w, h = ship["xywh"]
        cv2.rectangle(canvas, (round(x), round(y)), (round(x + w), round(y + h)), (80, 190, 230), 1)
    locked = status.get("locked_target")
    target_id = locked.get("target_id") if locked else None
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 58), (12, 20, 27), -1)
    cv2.putText(canvas, "FAKE LENS / PROJECTED DETECTIONS - NO OPTICAL VALIDATION", (9, 21), cv2.FONT_HERSHEY_SIMPLEX, .5, (230, 235, 240), 1)
    cv2.putText(canvas, f"source={timestamp:.2f}s view={view_factor:.1f}x phase={status['phase']} target={target_id} recorded={status['active_id'] is not None}",
        (9, 45), cv2.FONT_HERSHEY_SIMPLEX, .52, (150, 230, 240), 1)
    return canvas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--detections", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sample-fps", type=float, default=4)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--calibrated-factors", default="1,2,3,4,6,8")
    args = parser.parse_args()
    if not args.input.is_file() or not args.detections.is_file() or not .1 <= args.sample_fps <= 20:
        parser.error("valid input, detection cache and positive sample FPS are required")
    args.output.mkdir(parents=True, exist_ok=True)
    passages = args.output / "passages"
    if passages.is_dir() and list(passages.glob("*/manifest.json")):
        parser.error("output already contains a simulation; choose a new output directory")
    samples = [json.loads(line) for line in args.detections.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not samples or any(sample.get("clock") != "decoded_pts" for sample in samples):
        parser.error("cache must contain actual decoded source PTS; rerun simulate_adaptive_monitor.py --reuse-detections")
    factors = sorted({float(item) for item in args.calibrated_factors.split(",")})
    if 1 not in factors or len(factors) < 2 or not all(math.isfinite(item) and item >= 1 for item in factors):
        parser.error("simulated calibrated factors must include 1 and a larger finite factor")
    controller = FakeCalibratedLens(factors)
    manager = RecordingManager(passages)
    manager.set_zoom_controller(controller)
    overrides = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings else {}
    manager.update_settings({**overrides, "enabled": True, "recording_mode": "optical_cycle"})
    reader = cached_frames(args.input, samples)
    intermediate = args.output / "simulation.capture.mp4"
    writer = cv2.VideoWriter(str(intermediate), cv2.VideoWriter_fourcc(*"mp4v"), args.sample_fps, (960, 540))
    if not writer.isOpened():
        reader.close()
        writer.release()
        manager.finish("source_error")
        manager.wait_for_optical_stop()
        manager.control_executor.shutdown(wait=True)
        manager.executor.shutdown(wait=True)
        raise RuntimeError("cannot decode input or write the simulation video")
    timeline = []
    try:
        for index, (sample, original) in enumerate(reader):
            manager.control_executor.submit(lambda: None).result(timeout=10)
            context = manager.zoom_observation_context()
            factor = controller.factor
            image, vessels = project(original, sample["vessels"], factor)
            controller.source_seconds = sample["timestamp"]
            manager.process_frame(image, camera_id="simulation-camera", session_id="real-clip-simulation",
                source_name=args.input.name, fps=args.sample_fps, timestamp=sample["timestamp"],
                vessels=vessels, source_type="camera", observation_context=context)
            status = manager.status()
            timeline.append({"source_seconds": sample["timestamp"], "source_frame_index": sample["source_frame_index"],
                             "view_factor": factor, "projected_vessel_count": len(vessels), **status})
            annotated = overlay(image, vessels, status, sample["timestamp"], factor)
            writer.write(annotated)
            if index in {0, 25, 60, len(samples) - 1}:
                cv2.imwrite(str(args.output / f"simulation-{index:03d}.jpg"), annotated)
            if index % 20 == 0:
                print(json.dumps({"phase": "cycle_replay", "sample": index + 1, "source_seconds": sample["timestamp"], "state": status["phase"], "view_factor": factor}, ensure_ascii=False), flush=True)
        active_at_end = manager.status()["active_id"]
        manager.finish("source_completed")
        manager.wait_for_optical_stop(timeout=15)
        manager.executor.submit(lambda: None).result(timeout=30)
        items = manager.list_recordings(page_size=100)["items"]
        summary = {
            "input": str(args.input.resolve()), "sample_count": len(samples), "clock": "decoded_source_pts",
            "physical_camera_controlled": False, "detections_reinferred_after_zoom": False,
            "source_ended_with_active_cycle": active_at_end is not None,
            "naturally_completed_cycles": sum(item["reason"] == "vessel_left" for item in items),
            "saved_cycle_videos": len(items), "lens_commands": controller.commands,
            "cycles": [{key: item.get(key) for key in ("id", "status", "reason", "duration_seconds", "target_id", "initial_optical_zoom_factor", "optical_zoom_factor", "returned_to_overview", "selection_reason", "zoom_decisions")}
                       for item in items],
            "cycle_video_paths": [str(manager.media_path(item["id"], "cycle").resolve()) for item in items if item["cycle_url"]],
            "limitations": ["The source watermark already reports Z:3. Simulated 1x means this input's reference view, not calibrated camera optical 1x.",
                "All zoomed frames are digital crops and detections are projected from the original real detector output; no zoomed-view inference or optical image quality is validated.",
                "The source may end before a bow-to-stern pass completes. Source-end finalization must not be interpreted as observed stern departure.",
                "Only 106 sampled observations are available; this is a control-flow smoke test, not field-performance certification."],
        }
        (args.output / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        writer.release()
        reader.close()
        manager.finish("source_completed")
        manager.wait_for_optical_stop(timeout=15)
        manager.control_executor.shutdown(wait=True)
        manager.executor.shutdown(wait=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for browser-playable simulation")
    subprocess.run([ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-i", str(intermediate), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.output / "simulation.mp4")], check=True)
    intermediate.unlink()


if __name__ == "__main__":
    main()
