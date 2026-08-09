from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from app.schemas import Detection, DraftObservation, FrameResultV2, TaskKind, VesselFrameResult, sample_id_for
from app.pipeline import Pipeline
from app.storage import ResultStorage


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
