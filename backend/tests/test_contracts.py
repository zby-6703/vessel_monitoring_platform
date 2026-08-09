from datetime import datetime, timezone

import pytest

from app.schemas import (
    ALLOWED_INSTANCE_TRANSITIONS,
    ALLOWED_JOB_TRANSITIONS,
    AttributeStatus,
    Detection,
    InstanceState,
    JobStatus,
    assert_transition,
    sample_id_for,
)
from app.schemas import normalize_ship_name, recognized_name_fields


def test_task_and_instance_state_machines_reject_invalid_transitions():
    assert JobStatus.RUNNING in ALLOWED_JOB_TRANSITIONS[JobStatus.QUEUED]
    assert InstanceState.CONFIRMED in ALLOWED_INSTANCE_TRANSITIONS[InstanceState.TENTATIVE]
    assert_transition(JobStatus.QUEUED, JobStatus.RUNNING, ALLOWED_JOB_TRANSITIONS)
    with pytest.raises(ValueError):
        assert_transition(JobStatus.COMPLETED, JobStatus.RUNNING, ALLOWED_JOB_TRANSITIONS)


def test_identifiers_and_geometry_contract_are_stable():
    assert sample_id_for("ship.jpg") == "ship.jpg"
    assert sample_id_for("video.mp4", 12) == "video.mp4#frame=12"
    Detection(label="ship", xyxy=(1, 2, 3, 4), confidence=.5)
    with pytest.raises(ValueError):
        Detection(label="ship", xyxy=(3, 2, 1, 4), confidence=.5)


def test_ship_name_normalization_preserves_chinese_and_numbers():
    assert normalize_ship_name(" 贵 港 海泰 52 ") == "贵港海泰52"
    assert normalize_ship_name("  sea- 88 ") == "SEA88"
    assert recognized_name_fields("sea 88") == ("SEA88", "SEA88")
    assert recognized_name_fields("贵港海泰52") == ("贵港海泰52", "GUIGANGHAITAI52")
    assert recognized_name_fields(" ") == ("UNKNOWN", "UNKNOWN")
