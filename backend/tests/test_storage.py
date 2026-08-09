import json
from datetime import datetime, timezone

import numpy as np

from app.schemas import Detection, DraftObservation, FrameResultV2, TaskKind, VesselFrameResult, WaterPolygon
from app.storage import ResultStorage


def _frame():
    return FrameResultV2(task_id="task-1", source_filename="船.jpg", sample_id="船.jpg", observed_at=datetime.now(timezone.utc), image_width=20, image_height=10, processing_ms=1, vessels=[VesselFrameResult(vessel_id=1, ship=Detection(label="ship", xyxy=(1, 1, 10, 8), confidence=.9), water=[WaterPolygon(points=[(1, 1), (2, 1), (2, 2)])], recognized_zh="贵港海泰52", recognized_en="UNKNOWN", draft=DraftObservation(depth_m=2.3, success=True))])


def test_storage_writes_flat_labelme_and_compact_csv(tmp_path):
    storage = ResultStorage(tmp_path, "task-1")
    frame = _frame()
    label = storage.write_image_label(storage.source_stem(1, frame.source_filename), frame, np.zeros((10, 20, 3), dtype=np.uint8))
    storage.write_csv([{"filename": frame.source_filename, "recognized_zh": "贵港海泰52", "recognized_en": None, "draft_depth_m": 2.3}], TaskKind.FULL_PIPELINE)
    payload = json.loads(label.read_text(encoding="utf-8"))
    assert label.parent == tmp_path / "task-1"
    assert any(shape["label"] == "water" for shape in payload["shapes"])
    assert payload["imageData"] is None
    assert (storage.directory / "result.csv").is_file()
    assert not (storage.directory / "output").exists()
