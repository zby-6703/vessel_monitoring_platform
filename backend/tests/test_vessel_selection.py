"""Selection decisions use source geometry and motion evidence, not ship width."""
from __future__ import annotations

import math

import pytest
import numpy as np

from app.recording import RecordingSettings
from app.vessel_selection import VesselTargetSelector


WIDTH, HEIGHT = 1000, 600
FACTORS = [1, 2, 3, 5, 8]


def ship(ship_id=1, *, x=.2, y=None, width=.3, height=.2, confidence=.95, name=None):
    y = (.5 - height / 2) if y is None else y
    return {
        "ship_id": ship_id, "ship_name": name or f"船{ship_id}", "confidence": confidence,
        "boxes": [{"type": "ship", "confidence": confidence,
                   "xywh": [x * WIDTH, y * HEIGHT, width * WIDTH, height * HEIGHT]}],
    }


def settings(**values):
    defaults = dict(target_height_ratio=.6, adaptive_zoom_enabled=True,
                    min_optical_zoom=1.1, max_optical_zoom=8, suppress_stationary=False)
    defaults.update(values)
    return RecordingSettings(**defaults)


def update(selector, vessels, timestamp=0., *, config=None, zoom=1., factors=FACTORS, motion=None):
    return selector.update(vessels, WIDTH, HEIGHT, timestamp, config or settings(),
                           zoom_factor=zoom, calibrated_factors=factors, camera_motion=motion)


def target_for(result, ship_id):
    return next(target for target in result["targets"] if target["ship_id"] == ship_id)


def test_adaptive_magnification_uses_height_even_when_only_a_narrow_bow_is_visible():
    narrow = update(VesselTargetSelector(), [ship(width=.04)])
    wide = update(VesselTargetSelector(), [ship(width=.65)])
    assert narrow["requested_zoom_factor"] == pytest.approx(3)
    assert narrow["zoom_factor"] == 3
    assert wide["requested_zoom_factor"] == pytest.approx(narrow["requested_zoom_factor"])
    assert wide["zoom_factor"] == narrow["zoom_factor"]


def test_adaptive_zoom_uses_safe_calibrated_steps_and_never_fabricates_a_position():
    result = update(VesselTargetSelector(), [ship(height=.14)])
    assert result["requested_zoom_factor"] == pytest.approx(.6 / .14)
    assert result["zoom_factor"] == 3
    assert result["zoom_factor"] in FACTORS
    assert result["zoom_factor"] <= result["requested_zoom_factor"]


def test_already_large_ship_does_not_receive_an_unnecessary_zoom():
    result = update(VesselTargetSelector(), [ship(y=.1, height=.7)])
    assert result["zoom_factor"] == 1


def test_distant_ship_is_bounded_by_configured_maximum_zoom():
    config = settings(max_optical_zoom=3)
    result = update(VesselTargetSelector(), [ship(height=.03)], config=config)
    assert result["zoom_factor"] == 3


def test_missing_calibration_does_not_claim_optical_magnification():
    result = update(VesselTargetSelector(), [ship(height=.1)], factors=[])
    assert result["zoom_factor"] <= 1
    assert result["selection_reason"]


def test_ship_near_vertical_edge_is_not_zoomed_out_of_the_fixed_optical_view():
    result = update(VesselTargetSelector(), [ship(y=.02, height=.2)])
    assert result["zoom_factor"] == 1
    assert result["selection_reason"]


def test_nearest_candidate_is_the_largest_apparent_height_and_not_the_widest_ship():
    result = update(VesselTargetSelector(), [
        ship(1, x=.05, width=.7, height=.1),
        ship(2, x=.65, width=.08, height=.35),
    ])
    assert result["selected"]["ship_id"] == 2
    assert target_for(result, 2)["height_ratio"] == pytest.approx(.35)


def test_selection_hysteresis_prevents_similar_sized_targets_from_oscillating():
    selector = VesselTargetSelector()
    initial = update(selector, [ship(1, x=.05, width=.2, height=.3), ship(2, x=.65, width=.2, height=.28)])
    almost_equal = update(selector, [ship(1, x=.05, width=.2, height=.3), ship(2, x=.65, width=.2, height=.31)], .5)
    assert initial["selected"]["ship_id"] == 1
    assert almost_equal["selected_target_id"] == initial["selected_target_id"]
    # A single enlarged detection can be jitter. Sustain the larger hull long
    # enough for the short median-height window before testing a real switch.
    for timestamp in [1, 1.5, 2]:
        clearly_nearer = update(selector, [ship(1, x=.05, width=.2, height=.3), ship(2, x=.65, width=.2, height=.42)], timestamp)
    assert clearly_nearer["selected"]["ship_id"] == 2


def motion_settings(**overrides):
    base = settings().model_dump()
    base.update(suppress_stationary=True, stationary_seconds=3,
                stationary_motion_threshold=.015, motion_confirm_seconds=1)
    base.update(overrides)
    return RecordingSettings(**base)


def test_startup_does_not_immediately_grant_a_stationary_near_ship_the_lens():
    selector = VesselTargetSelector()
    config = motion_settings()
    first = update(selector, [ship(1, height=.35)], config=config)
    assert first["selected"] is None
    assert target_for(first, 1)["motion_state"] == "observing"
    for index in range(1, 10):
        result = update(selector, [ship(1, height=.35)], index * .5, config=config)
    parked = target_for(result, 1)
    assert parked["motion_state"] == "stationary"
    assert parked["eligible"] is False
    assert result["selected"] is None


def test_stationary_near_ship_does_not_hide_a_new_moving_far_ship():
    selector = VesselTargetSelector()
    config = motion_settings()
    for index in range(9):
        update(selector, [ship(1, x=.05, width=.25, height=.35)], index * .5, config=config)
    for index in range(6):
        result = update(selector, [
            ship(1, x=.05, width=.25, height=.35),
            ship(2, x=.55 + index * .025, width=.15, height=.12),
        ], 4.5 + index * .5, config=config)
    assert target_for(result, 1)["motion_state"] == "stationary"
    assert result["selected"]["ship_id"] == 2


def test_box_jitter_and_upstream_track_id_changes_do_not_release_a_parked_ship():
    selector = VesselTargetSelector()
    config = motion_settings()
    identities = set()
    for index in range(14):
        x = .2 + (.001 if index % 2 else -.001)
        result = update(selector, [ship(100 + index, x=x)], index * .5, config=config)
        identities.add(result["targets"][0]["target_id"])
    assert len(identities) == 1
    assert result["targets"][0]["motion_state"] == "stationary"
    assert result["selected"] is None


def test_long_unobserved_gap_is_not_counted_as_stationary_evidence():
    selector = VesselTargetSelector()
    config = motion_settings()
    update(selector, [ship()], 0, config=config)
    update(selector, [ship()], .5, config=config)
    result = update(selector, [ship()], 60, config=config)
    assert result["targets"][0]["motion_state"] != "stationary"
    assert result["targets"][0]["stationary_seconds"] < 3


def test_a_previously_parked_ship_becomes_eligible_after_confirmed_departure_motion():
    selector = VesselTargetSelector()
    config = motion_settings()
    for index in range(9):
        update(selector, [ship()], index * .5, config=config)
    for index in range(1, 9):
        result = update(selector, [ship(x=.2 + index * .025)], 4 + index * .5, config=config)
    assert result["targets"][0]["motion_state"] == "moving"
    assert result["selected"]["ship_id"] == 1


def test_optical_zoomed_height_is_normalized_back_to_the_one_x_plane():
    result = update(VesselTargetSelector(), [ship(x=.05, y=.2, width=.8, height=.6)], zoom=3)
    assert result["targets"][0]["height_ratio"] == pytest.approx(.2)
    assert result["requested_zoom_factor"] == pytest.approx(3)


def test_background_pan_is_not_mistaken_for_a_parked_ship_starting_to_move():
    selector = VesselTargetSelector()
    config = motion_settings()
    for index in range(14):
        camera_motion = {"dx": .02 if index else 0, "dy": 0, "scale": 1,
                         "valid": True, "elapsed_seconds": .5}
        result = update(selector, [ship(x=.2 + index * .02)], index * .5,
                        config=config, motion=camera_motion)
    assert result["targets"][0]["motion_state"] == "stationary"
    assert result["selected"] is None


def test_unreliable_background_motion_does_not_accumulate_stationary_evidence():
    selector = VesselTargetSelector()
    config = motion_settings()
    for index in range(14):
        result = update(selector, [ship()], index * .5, config=config,
                        motion={"dx": 0, "dy": 0, "scale": 1, "valid": False, "elapsed_seconds": .5})
    assert result["targets"][0]["motion_state"] != "stationary"
    assert result["targets"][0]["stationary_seconds"] < 3


@pytest.mark.parametrize("bad", [
    {"ship_id": 1, "confidence": math.nan, "boxes": [{"type": "ship", "xywh": [0, 0, 20, 20]}]},
    {"ship_id": 1, "confidence": .9, "boxes": [{"type": "ship", "xywh": [0, 0, math.nan, 20]}]},
    {"ship_id": 1, "confidence": .9, "boxes": [{"type": "ship", "xywh": [0, 0, -1, 20]}]},
    {"ship_id": 1, "confidence": .9, "boxes": [{"type": "ship", "xywh": [0, 0, 0, 20]}]},
])
def test_invalid_detections_cannot_receive_the_camera(bad):
    result = update(VesselTargetSelector(), [bad])
    assert result["selected"] is None
    assert result["targets"] == []


def test_distinctive_hull_color_blocks_a_reused_detector_id_from_hijacking_identity():
    selector = VesselTargetSelector()
    vessel = ship(x=.4, y=.4, width=.2, height=.2)
    red = np.full((HEIGHT, WIDTH, 3), 100, dtype=np.uint8)
    red[240:360, 400:600] = (20, 20, 220)
    blue = red.copy()
    blue[240:360, 400:600] = (220, 30, 20)
    initial = selector.update([vessel], WIDTH, HEIGHT, 0, settings(), image=red)
    replaced = selector.update([vessel], WIDTH, HEIGHT, .5, settings(), image=blue)
    assert replaced["targets"][0]["target_id"] != initial["targets"][0]["target_id"]


def test_ambiguous_overlapping_geometry_is_not_granted_the_lens():
    selector = VesselTargetSelector()
    update(selector, [ship(1, x=.3), ship(2, x=.305)])
    result = update(selector, [ship(101, x=.301), ship(102, x=.304)], .5)
    assert result["selected"] is None
    assert all(target["identity_ambiguous"] for target in result["targets"])


def test_a_single_bbox_jump_followed_by_stability_does_not_unpark_a_vessel():
    selector = VesselTargetSelector()
    config = motion_settings()
    for index in range(10):
        update(selector, [ship(x=.2)], index * .5, config=config)
    for index in range(1, 9):
        result = update(selector, [ship(x=.235)], 4.5 + index * .5, config=config)
        assert result["selected"] is None
