from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from app.schemas import Detection, DraftObservation, FrameResultV2, TaskKind, VesselFrameResult, sample_id_for
from app.pipeline import Pipeline
from app.storage import ResultStorage
import pytest
from datetime import timedelta
from types import SimpleNamespace


class FakeFrameInference:
    def infer(self, frame, **kwargs):
        return FrameResultV2(task_id=kwargs["task_id"], source_filename=kwargs["source_filename"], sample_id=sample_id_for(kwargs["source_filename"], kwargs.get("frame_index")), frame_index=kwargs.get("frame_index"), source_frame_index=kwargs.get("source_frame_index"), observed_at=kwargs["observed_at"], image_width=frame.shape[1], image_height=frame.shape[0], processing_ms=0, vessels=[VesselFrameResult(vessel_id=1, ship=Detection(label="ship", xyxy=(1, 1, 4, 4), confidence=.9), recognized_zh="TEST01", recognized_en="TEST01", draft=DraftObservation(depth_m=2.1, success=True))])


def test_image_pipeline_writes_only_flat_v2_outputs(tmp_path: Path):
    image = tmp_path / "one.jpg"
    cv2.imwrite(str(image), np.zeros((8, 8, 3), dtype=np.uint8))
    storage = ResultStorage(tmp_path / "results", "task")
    outcome = Pipeline(FakeFrameInference(), storage).process_file(image, ordinal=1, task=TaskKind.FULL_PIPELINE, enable_tracking=False)
    storage.write_csv(outcome["csv_rows"], TaskKind.FULL_PIPELINE)
    assert (storage.directory / "001_one.json").is_file()
    assert (storage.directory / "result.csv").is_file()
    assert not (storage.directory / "output").exists()


def test_video_pipeline_writes_one_jsonl_and_compact_frame_csv(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (8, 8))
    for _ in range(3): writer.write(np.zeros((8, 8, 3), dtype=np.uint8))
    writer.release()
    storage = ResultStorage(tmp_path / "results", "task")
    outcome = Pipeline(FakeFrameInference(), storage).process_file(video, ordinal=1, task=TaskKind.FULL_PIPELINE, enable_tracking=False, frame_step=2)
    storage.write_csv(outcome["csv_rows"], TaskKind.FULL_PIPELINE, video_untracked=True)
    assert len((storage.directory / "001_clip.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert (storage.directory / "result.csv").read_text(encoding="utf-8-sig").splitlines()[0] == "frame_index,recognized_zh,recognized_en,draft_depth_m"
    assert not list(storage.directory.glob("*.mp4"))


def test_video_visualization_is_opt_in(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (8, 8)); writer.write(np.zeros((8, 8, 3), dtype=np.uint8)); writer.release()
    storage = ResultStorage(tmp_path / "results", "task")
    Pipeline(FakeFrameInference(), storage).process_file(video, ordinal=1, task=TaskKind.FULL_PIPELINE, enable_tracking=False, visualize=True)
    assert (storage.directory / "001_clip_annotated.mp4").is_file()


def test_passage_analysis_preserves_capture_time_and_source_fps(tmp_path):
    video = tmp_path / "passage.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 12, (32, 24))
    for _ in range(6): writer.write(np.zeros((24, 32, 3), np.uint8))
    writer.release()
    storage = ResultStorage(tmp_path / "results", "passage-job")
    started = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    outcome = Pipeline(FakeFrameInference(), storage).process_file(video, ordinal=1, task=TaskKind.FULL_PIPELINE,
        enable_tracking=False, frame_step=2, observed_start=started, visualize=True)
    import json
    frames = [json.loads(line) for line in (storage.directory / "001_passage.jsonl").read_text().splitlines()]
    assert datetime.fromisoformat(frames[0]["observed_at"].replace("Z", "+00:00")) == started
    assert datetime.fromisoformat(frames[1]["observed_at"].replace("Z", "+00:00")) == started + timedelta(seconds=2 / 12)
    cap = cv2.VideoCapture(str(storage.result_root / outcome["visual_uri"]))
    assert cap.get(cv2.CAP_PROP_FPS) == pytest.approx(6)
    assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 3
    cap.release()


def test_failed_model_is_not_a_successful_offline_analysis(tmp_path):
    image = tmp_path / "frame.jpg"
    cv2.imwrite(str(image), np.zeros((24, 32, 3), np.uint8))
    class Failed(FakeFrameInference):
        def infer(self, *args, **kwargs):
            result = super().infer(*args, **kwargs)
            result.errors = ["draft_estimation_failed: model unavailable"]
            return result
    storage = ResultStorage(tmp_path / "results", "failed-job")
    with pytest.raises(RuntimeError, match="model unavailable"):
        Pipeline(Failed(), storage).process_file(image, ordinal=1, task=TaskKind.FULL_PIPELINE, enable_tracking=False)


def test_finished_track_is_in_csv_even_when_it_left_before_video_end(tmp_path, monkeypatch):
    from app import pipeline as module
    started = datetime(2026, 11, 1, tzinfo=timezone.utc)
    samples = [module.VideoSample(0, 0, started, np.zeros((24, 32, 3), np.uint8)),
               module.VideoSample(1, 1, started + timedelta(seconds=10), np.zeros((24, 32, 3), np.uint8))]
    monkeypatch.setattr(module, "sample_video", lambda *args, **kwargs: iter(samples))
    class FirstFrameOnly(FakeFrameInference):
        def infer(self, *args, **kwargs):
            result = super().infer(*args, **kwargs)
            if kwargs["frame_index"]: result.vessels = []
            return result
    tracker = SimpleNamespace(update=lambda *args, **kwargs: [SimpleNamespace(track_id=1, tlbr=(1, 1, 4, 4))])
    storage = ResultStorage(tmp_path / "results", "tracked-job")
    repository_calls = []
    repository = SimpleNamespace(save_video_instance=lambda *args: repository_calls.append(args))
    outcome = Pipeline(FirstFrameOnly(), storage, repository, tracker_factory=lambda: tracker).process_file(
        tmp_path / "passage.mp4", ordinal=1, task=TaskKind.FULL_PIPELINE, enable_tracking=True)
    assert [row["instance_id"] for row in outcome["csv_rows"]] == [1]
    assert len(repository_calls) == 1
