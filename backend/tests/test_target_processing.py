from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.inference.frame_inference import FrameInferenceService
from app.recording import RecordingSettings
from app.schemas import Detection, FrameResultV2, VesselFrameResult
from app.target_processing import RealtimeTargetProcessor


IMAGE = np.zeros((1000, 1000, 3), dtype=np.uint8)


class Models:
    def __init__(self):
        self.name_calls = self.draft_calls = 0
        self.name_recognizer = self.name
        self.draft_predictor = SimpleNamespace(predict=self.draft)
        self.depth_estimator = SimpleNamespace(estimate_with_details=lambda *args: SimpleNamespace(depth=None, success=False))

    def name(self, **kwargs):
        self.name_calls += 1
        return [{"text": "TEST1", "score": .9}]

    def draft(self, image):
        self.draft_calls += 1
        return SimpleNamespace(detection=SimpleNamespace(boxes=[]),
            segmentation=SimpleNamespace(get_binary_mask=lambda **kwargs: np.zeros(image.shape[:2], dtype=np.uint8)))


def vessel(box=(300, 400, 700, 550), track=1, confidence=.9):
    x1, y1, x2, y2 = box
    return VesselFrameResult(vessel_id=track, track_id=track,
        ship=Detection(label="ship", xyxy=box, confidence=confidence),
        regions=[Detection(label="ship_name_area", xyxy=(x1, y1, min(x2, x1 + 10), min(y2, y1 + 10)), confidence=.9),
                 Detection(label="waterline_area", xyxy=(x1, y1, min(x2, x1 + 20), min(y2, y1 + 20)), confidence=.9)])


def frame(vessels):
    return FrameResultV2(task_id="test", source_filename="camera", sample_id="sample",
        observed_at=datetime.now(timezone.utc), image_width=1000, image_height=1000,
        processing_ms=1., vessels=vessels)


def policy(**kwargs):
    return RecordingSettings(suppress_stationary=False, **kwargs)


def fixed_background(processor, valid=True):
    processor.background.update = lambda *args, **kwargs: {"valid": valid, "dx": 0., "dy": 0., "scale": 1.}


def run(processor, service, timestamp, vessels=None, settings=None, context=None):
    result = frame(vessels if vessels is not None else [vessel()])
    processor.process(service, result, IMAGE, timestamp, settings or policy(), context)
    return result


@pytest.fixture
def setup():
    models = Models()
    processor = RealtimeTargetProcessor()
    fixed_background(processor)
    return processor, FrameInferenceService(models), models


def test_small_targets_never_call_name_or_draft_but_keep_track(setup):
    processor, service, models = setup
    for timestamp in (0, .2, .4, .8, 1.2):
        result = run(processor, service, timestamp, [vessel((300, 400, 380, 420))])
    assert result.vessels[0].track_id == 1
    assert result.vessels[0].processing["reason"] == "small_target"
    assert result.processing_summary == {"detected": 1, "eligible": 0, "detail_inferred": 0, "lightweight": 1, "detail_skipped": 0}
    assert models.name_calls == models.draft_calls == 0


def test_stable_large_target_only_enriches_after_hits_and_time(setup):
    processor, service, models = setup
    assert run(processor, service, 0).vessels[0].processing["reason"] == "waiting_stability"
    assert run(processor, service, .2).vessels[0].processing["reason"] == "waiting_stability"
    result = run(processor, service, .4)
    assert result.vessels[0].recognized_zh == "TEST1"
    assert result.vessels[0].track_id == 1
    assert result.vessels[0].processing["detail_inferred"]
    assert models.name_calls == models.draft_calls == 1


def test_interval_and_budget_keep_eligible_targets_while_rotating_work(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0, detail_interval_seconds=.8)
    boats = lambda: [vessel((100, 300, 400, 500), 1), vessel((600, 300, 900, 450), 2)]
    first = run(processor, service, 0, boats(), settings)
    assert first.vessels[0].processing["detail_inferred"]
    assert first.vessels[1].processing["reason"] == "detail_budget"
    second = run(processor, service, .2, boats(), settings)
    assert second.vessels[0].processing["reason"] == "detail_cooldown"
    assert second.vessels[1].processing["detail_inferred"]
    third = run(processor, service, .4, boats(), settings)
    assert third.processing_summary["eligible"] == 2
    assert third.processing_summary["detail_inferred"] == 0
    assert models.name_calls == models.draft_calls == 2


def test_zero_interval_does_not_starve_smaller_large_ship(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0, detail_interval_seconds=0)
    boats = lambda: [vessel((100, 300, 400, 500), 1), vessel((600, 300, 900, 450), 2)]
    first = run(processor, service, 0, boats(), settings)
    second = run(processor, service, .2, boats(), settings)
    assert first.vessels[0].processing["detail_inferred"]
    assert second.vessels[1].processing["detail_inferred"]


def test_roi_applies_before_detail_inference(setup):
    processor, service, models = setup
    settings = policy(target_roi_left=.7, target_confirm_hits=1, target_confirm_seconds=0)
    result = run(processor, service, 0, settings=settings)
    assert result.vessels[0].processing["reason"] == "outside_monitoring_area"
    assert models.name_calls == models.draft_calls == 0


def test_horizontal_entry_uses_height_even_before_full_width_visible(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0)
    result = run(processor, service, 0, [vessel((0, 400, 30, 550))], settings)
    assert result.vessels[0].processing["eligible"]
    small = run(processor, service, .2, [vessel((0, 400, 30, 420), 2)], settings)
    assert small.vessels[0].processing["reason"] == "small_target"
    assert models.name_calls == models.draft_calls == 1


def test_established_bow_entry_survives_moving_a_few_pixels_inside_before_confirmation(setup):
    processor, service, models = setup
    for timestamp, x, width in ((0, 0, 30), (.2, 5, 35), (.4, 10, 40)):
        result = run(processor, service, timestamp, [vessel((x, 400, x + width, 480))])
    assert result.vessels[0].processing["eligible"]
    assert result.vessels[0].processing["width_ratio"] < .12
    assert result.vessels[0].processing["horizontal_entry_exemption"]
    assert models.name_calls == models.draft_calls == 1


def test_narrow_interior_target_does_not_inherit_entry_from_reused_id(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0)
    first = run(processor, service, 0, [vessel((0, 400, 30, 480))], settings)
    result = run(processor, service, .2, [vessel((50, 400, 80, 480))], settings)
    # The selector permits this nearby same-ID association; the admission
    # history must still reject transferring the old partial-entry privilege.
    assert result.vessels[0].processing["target_id"] == first.vessels[0].processing["target_id"]
    assert result.vessels[0].processing["reason"] == "small_target"
    assert not result.vessels[0].processing["horizontal_entry_exemption"]
    assert models.name_calls == models.draft_calls == 1


def test_true_observation_gap_clears_old_edge_exemption(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0)
    run(processor, service, 0, [vessel((0, 400, 30, 480))], settings)
    result = run(processor, service, 3., [vessel((5, 400, 40, 480))], settings)
    assert result.vessels[0].processing["reason"] == "small_target"
    assert not result.vessels[0].processing["horizontal_entry_exemption"]
    assert models.name_calls == models.draft_calls == 1


def test_narrow_hull_without_continued_growth_cannot_keep_entry_privilege(setup):
    processor, service, models = setup
    for timestamp, x in ((0, 0), (.2, 5), (.4, 10)):
        result = run(processor, service, timestamp, [vessel((x, 400, x + 30, 480))])
    assert result.vessels[0].processing["eligible"]
    result = run(processor, service, 2.1, [vessel((10, 400, 40, 480))])
    assert result.vessels[0].processing["reason"] == "small_target"
    assert not result.vessels[0].processing["horizontal_entry_exemption"]


def test_full_width_confirmation_closes_partial_entry_privilege(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0)
    run(processor, service, 0, [vessel((0, 400, 30, 480))], settings)
    for timestamp in (.2, .4, .6):
        result = run(processor, service, timestamp, [vessel((5, 400, 155, 480))], settings)
    assert result.vessels[0].processing["eligible"]
    assert not result.vessels[0].processing["horizontal_entry_exemption"]


def test_partial_entry_still_obeys_height_roi_and_stationary_rules(setup):
    processor, service, models = setup
    settings = RecordingSettings(suppress_stationary=True, stationary_seconds=3,
        target_confirm_hits=1, target_confirm_seconds=0, detail_interval_seconds=0)
    for timestamp in (0, .5, 1., 1.5, 2., 2.5, 3.):
        # Slowly changing visible width keeps entry evidence fresh but cannot
        # establish a sustained displacement large enough to prove motion.
        result = run(processor, service, timestamp,
            [vessel((0, 400, 30 + 5 * timestamp, 480))], settings)
    assert result.vessels[0].processing["horizontal_entry_exemption"]
    assert result.vessels[0].processing["reason"] == "stationary_excluded"
    outside = RealtimeTargetProcessor()
    result = run(outside, service, 0, [vessel((0, 400, 30, 480))],
        policy(target_roi_left=.1, target_confirm_hits=1, target_confirm_seconds=0))
    assert result.vessels[0].processing["reason"] == "outside_monitoring_area"


@pytest.mark.parametrize("context,reason", [
    ({"zoom_factor": 3., "accept_detections": False, "generation": 1}, "lens_moving"),
    ({"zoom_factor": None, "accept_detections": True, "generation": 1}, "zoom_unknown"),
])
def test_unsettled_or_unknown_lens_never_runs_detail(setup, context, reason):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0)
    result = run(processor, service, 0, settings=settings, context=context)
    assert result.vessels[0].processing["reason"] == reason
    assert models.name_calls == models.draft_calls == 0


def test_optical_enlargement_does_not_promote_small_ship(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0)
    result = run(processor, service, 0, [vessel((400, 400, 600, 550))], settings,
        {"zoom_factor": 3., "accept_detections": True, "generation": 1})
    assert result.vessels[0].processing["height_ratio"] == pytest.approx(.05)
    assert result.vessels[0].processing["reason"] == "small_target"
    assert models.name_calls == models.draft_calls == 0


def test_missing_frame_gap_and_generation_require_new_stability(setup):
    processor, service, models = setup
    run(processor, service, 0)
    run(processor, service, .2, [])
    result = run(processor, service, .4)
    assert result.vessels[0].processing["hits"] == 1
    run(processor, service, .6)
    assert run(processor, service, .8).vessels[0].processing["eligible"]
    assert run(processor, service, 5).vessels[0].processing["reason"] == "waiting_stability"
    assert run(processor, service, 5.2, context={"zoom_factor": 1., "accept_detections": True, "generation": 1}).vessels[0].processing["hits"] == 1


def test_updated_policy_drops_old_qualification(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0)
    assert run(processor, service, 0, settings=settings).vessels[0].processing["eligible"]
    settings = policy(min_target_height_ratio=.2, target_confirm_hits=1, target_confirm_seconds=0)
    result = run(processor, service, .2, settings=settings)
    assert result.vessels[0].processing["reason"] == "small_target"
    assert models.name_calls == models.draft_calls == 1


def test_stationary_target_excluded_after_reliable_evidence(setup):
    processor, service, models = setup
    settings = RecordingSettings(suppress_stationary=True, stationary_seconds=3,
        target_confirm_hits=1, target_confirm_seconds=0, detail_interval_seconds=0)
    for timestamp in (0, .5, 1., 1.5, 2., 2.5):
        run(processor, service, timestamp, settings=settings)
    result = run(processor, service, 3., settings=settings)
    assert result.vessels[0].processing["reason"] == "stationary_excluded"
    assert result.vessels[0].processing["motion_state"] == "stationary"
    assert models.name_calls == 6


def test_uncertain_background_does_not_block_stable_detail_or_prove_berth(setup):
    processor, service, models = setup
    fixed_background(processor, valid=False)
    settings = RecordingSettings(suppress_stationary=True, stationary_seconds=3,
        target_confirm_hits=1, target_confirm_seconds=0, detail_interval_seconds=0)
    for timestamp in (0, 1., 2., 3., 4.):
        result = run(processor, service, timestamp, settings=settings)
    assert result.vessels[0].processing["eligible"]
    assert result.vessels[0].processing["motion_state"] == "observing"
    assert models.name_calls == 5


def test_disabled_filter_restores_all_full_inference(setup):
    processor, service, models = setup
    settings = policy(target_filter_enabled=False)
    for timestamp in (0, .1):
        result = run(processor, service, timestamp,
            [vessel((300, 400, 320, 410), 1), vessel((700, 300, 720, 310), 2)], settings,
            {"zoom_factor": None, "accept_detections": False})
    assert result.processing_summary["detail_inferred"] == 2
    assert all(value.processing["reason"] == "filter_disabled" for value in result.vessels)
    assert models.name_calls == models.draft_calls == 4


def test_qualified_size_uses_hysteresis_but_new_small_target_does_not(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0, detail_interval_seconds=0)
    assert run(processor, service, 0, [vessel((300, 400, 700, 470))], settings).vessels[0].processing["eligible"]
    for timestamp in (.2, .4, .6, .8, 1.):
        result = run(processor, service, timestamp, [vessel((300, 400, 700, 455))], settings)
    assert result.vessels[0].processing["height_ratio"] == pytest.approx(.055)
    assert result.vessels[0].processing["eligible"]
    other = RealtimeTargetProcessor()
    result = run(other, service, 0, [vessel((300, 400, 700, 455))], settings)
    assert result.vessels[0].processing["reason"] == "small_target"


def test_zoom_crop_preserves_previously_measured_width(setup):
    processor, service, models = setup
    settings = policy(min_target_width_ratio=.45, target_confirm_hits=1,
        target_confirm_seconds=0, detail_interval_seconds=0)
    initial = run(processor, service, 0, [vessel((250, 400, 750, 550))], settings)
    target_id = initial.vessels[0].processing["target_id"]
    for timestamp in (.2, .4, .6, .8, 1.):
        result = run(processor, service, timestamp, [vessel((0, 200, 1000, 650))], settings,
            {"zoom_factor": 3., "accept_detections": True, "generation": 1})
    assert result.vessels[0].processing["target_id"] == target_id
    assert result.vessels[0].processing["width_ratio"] == pytest.approx(.5)
    assert result.vessels[0].processing["eligible"]


def test_roi_uses_overview_position_while_zoomed(setup):
    processor, service, models = setup
    settings = policy(target_roi_bottom=.62, target_confirm_hits=1, target_confirm_seconds=0)
    result = run(processor, service, 0, [vessel((200, 500, 800, 950))], settings,
        {"zoom_factor": 3., "accept_detections": True, "generation": 1})
    # Current-view centre .725 becomes .575 in 1x, inside the configured ROI.
    assert result.vessels[0].processing["eligible"]


def test_detection_confidence_excludes_before_detail(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0)
    result = run(processor, service, 0, [vessel(confidence=.35)], settings)
    assert result.vessels[0].processing["reason"] == "low_confidence"
    assert models.name_calls == models.draft_calls == 0


def test_real_lens_transition_retains_measured_hull_and_reconfirms_stability(setup):
    processor, service, models = setup
    settings = policy(min_target_width_ratio=.45, target_confirm_hits=3,
        target_confirm_seconds=.4, detail_interval_seconds=0)
    for timestamp in (0, .2, .4):
        initial = run(processor, service, timestamp, [vessel((250, 400, 750, 550))], settings)
    target_id = initial.vessels[0].processing["target_id"]
    assert initial.vessels[0].processing["eligible"]
    for timestamp in (.6, .8, 1.):
        result = run(processor, service, timestamp, settings=settings,
            context={"zoom_factor": None, "accept_detections": False, "generation": 1})
        assert result.vessels[0].processing["reason"] == "lens_moving"
    for timestamp in (1.2, 1.4, 1.6):
        result = run(processor, service, timestamp, [vessel((0, 200, 1000, 650))], settings,
            {"zoom_factor": 3., "accept_detections": True, "generation": 2})
        assert result.vessels[0].processing["target_id"] == target_id
        assert result.vessels[0].processing["width_ratio"] == pytest.approx(.5)
        if timestamp < 1.6:
            assert result.vessels[0].processing["reason"] == "waiting_stability"
    assert result.vessels[0].processing["eligible"]
    assert models.name_calls == models.draft_calls == 2


def test_new_arrivals_do_not_always_jump_a_waiting_existing_target(setup):
    processor, service, models = setup
    settings = policy(target_confirm_hits=1, target_confirm_seconds=0, detail_interval_seconds=0)
    first = run(processor, service, 0, [vessel((100, 300, 400, 450), 1)], settings)
    assert first.vessels[0].processing["detail_inferred"]
    existing_id = first.vessels[0].processing["target_id"]
    # Each new, larger candidate gets a new identity, but an existing due
    # candidate cannot be postponed forever by never-serviced arrivals.
    for ordinal in range(1, 6):
        # Model a high-churn arrival stream independently of the selector's
        # deliberate retention of identities at the same screen position.
        processor.selector.tracks = {existing_id: processor.selector.tracks[existing_id]}
        result = run(processor, service, ordinal * .2,
            [vessel((100, 300, 400, 450), 1), vessel((600, 300, 900, 550), ordinal + 10)], settings)
    assert any(value.processing["detail_inferred"] for value in result.vessels)
    assert result.vessels[0].processing["detail_inferred"]
    assert models.name_calls == models.draft_calls == 6


def test_epoch_timestamp_rounding_does_not_delay_confirmation_or_detail_interval(setup):
    processor, service, models = setup
    epoch = 1_790_000_000.
    for offset in (0, .2, .4):
        result = run(processor, service, epoch + offset)
    assert result.vessels[0].processing["detail_inferred"]
    for offset in (.6, .8, 1., 1.2):
        result = run(processor, service, epoch + offset)
    assert result.vessels[0].processing["detail_inferred"]
    assert models.name_calls == models.draft_calls == 2
