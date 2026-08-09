from dataclasses import replace
from app.config import Settings
from app.jobs import JobManager
from app.schemas import TaskKind
from app.storage import ResultStorage


def test_new_job_uses_flat_results_and_separate_upload_root(tmp_path):
    manager = JobManager(replace(Settings(), result_root=tmp_path / "results", upload_root=tmp_path / "uploads"))
    job = manager.create([("ship.jpg", b"data"), ("ship.jpg", b"data2")], {"task": "full_pipeline", "enable_tracking": False, "frame_step": 1, "max_frames": None, "visualize": False})
    assert (tmp_path / "results" / job["id"] / "job.json").is_file()
    assert (tmp_path / "uploads" / job["id"] / "001_ship.jpg").is_file()
    assert (tmp_path / "uploads" / job["id"] / "002_ship.jpg").is_file()
    manager.stop()


def test_untracked_video_job_uses_frame_csv_columns(tmp_path):
    storage = ResultStorage(tmp_path / "results", "task")
    job = {
        "items": [{"filename": "clip.mp4"}],
        "options": {"task": TaskKind.FULL_PIPELINE.value, "enable_tracking": False},
    }
    JobManager._write_result_csv(
        storage,
        [{"frame_index": 0, "recognized_zh": "TEST", "recognized_en": "TEST", "draft_depth_m": 2.1}],
        job,
    )
    assert (storage.directory / "result.csv").read_text(encoding="utf-8-sig").splitlines()[0] == "frame_index,recognized_zh,recognized_en,draft_depth_m"


def test_a_failed_file_does_not_stop_later_files(tmp_path):
    class FakePipeline:
        def process_file(self, source, **_):
            if source.name.startswith("001_"):
                raise ValueError("broken input")
            return {"result_uri": "task/002_ok.json", "visual_uri": None, "csv_rows": [{"filename": "ok.jpg", "recognized_zh": "TEST", "recognized_en": "TEST", "draft_depth_m": 2.1}]}

    manager = JobManager(replace(Settings(), result_root=tmp_path / "results", upload_root=tmp_path / "uploads"))
    manager._pipeline = lambda _: FakePipeline()
    job = manager.create(
        [("bad.jpg", b"data"), ("ok.jpg", b"data")],
        {"task": "full_pipeline", "enable_tracking": False, "frame_step": 1, "max_frames": None, "visualize": False},
    )
    manager._run(job["id"])
    result = manager.get(job["id"])
    assert result["status"] == "completed"
    assert result["items"][0]["status"] == "failed"
    assert result["items"][1]["status"] == "completed"
    manager.stop()
