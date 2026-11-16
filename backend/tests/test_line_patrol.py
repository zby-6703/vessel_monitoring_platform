"""Two image spaces, symmetric travel, and one file across repeated passes."""
import cv2
import pytest
from pydantic import ValidationError

from app.recording import RecordingSettings
from app.vessel_selection import VesselTargetSelector
from test_optical_cycle import cycle_factory


def boat(x, width=.5, *, reverse=False, ship_id=1):
    if reverse:
        x = 1 - x - width
    return {"ship_id": ship_id, "confidence": .95, "ship_name": "TEST",
            "boxes": [{"type": "ship", "xywh": [x * 64, .42 * 48, width * 64, .16 * 48]}]}


def patrol(factory, **overrides):
    return factory(line_patrol_enabled=True, auto_horizontal_follow=True,
                   zoom_view_limits_enabled=False, **overrides)


def acquire(h, x, *, reverse=False):
    for _ in range(4):
        h.feed([boat(x, reverse=reverse)], advance=.2)
        h.drain_control()


def cross_entry(h, x=-.15, width=.5, *, reverse=False):
    for _ in range(2): h.feed([boat(x, width, reverse=reverse)], advance=.2)
    h.phase("zoomed")


@pytest.mark.parametrize("reverse", [False, True])
def test_overview_bow_crosses_entry_line_before_zoom_without_overview_pan(cycle_factory, reverse):
    h = patrol(cycle_factory)
    h.warm()
    h.feed([boat(-.25, reverse=reverse)])
    rid = h.manager.active["id"]
    h.feed([boat(-.2, reverse=reverse)])
    assert h.controller.calls == [1] and not h.controller.pan_calls
    h.feed([boat(-.15, reverse=reverse)])
    assert h.controller.calls == [1]
    h.feed([boat(-.15, reverse=reverse)])
    h.phase("zoomed")
    assert h.controller.calls == [1, 3] and not h.controller.pan_calls
    assert h.manager.active["id"] == rid
    assert h.manager.status()["line_patrol"]["direction"] == ("right_to_left" if reverse else "left_to_right")


@pytest.mark.parametrize("reverse", [False, True])
def test_detail_waits_for_stern_line_then_pans_only_in_travel_direction(cycle_factory, reverse, monkeypatch):
    h = patrol(cycle_factory, pan_max_left_travel_ratio=.08, pan_max_right_travel_ratio=.21)
    h.warm()
    h.feed([boat(-.25, reverse=reverse)])
    cross_entry(h, reverse=reverse)
    acquire(h, .38, reverse=reverse)  # at 3x, tail=.14 (reverse=.86)
    assert not h.controller.pan_calls
    calls = []
    original = h.controller.follow_horizontal
    def pan(step, **kwargs):
        calls.append((step, kwargs))
        return original(step, **kwargs)
    monkeypatch.setattr(h.controller, "follow_horizontal", pan)
    # The first line along travel is 1/3 (2/3 when reversed).
    for x in (.42, .445):
        h.feed([boat(x, reverse=reverse)], advance=.5); h.drain_control()
    assert calls
    assert all((step < 0) == reverse for step, _ in calls)
    assert calls[0][1]["max_travel_ratio"] == (.08 if reverse else .21)
    assert h.controller.calls == [1, 3]


@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_zoom_passes_keep_one_file_until_original_view_stern_leaves(cycle_factory, reverse, monkeypatch):
    h = patrol(cycle_factory, real_video=not reverse)
    h.warm()
    h.feed([boat(-.25, reverse=reverse)])
    rid = h.manager.active["id"]
    cross_entry(h, reverse=reverse)
    acquire(h, .38, reverse=reverse)
    # Reach the range limit, then retain enlarged footage while visible.
    def limited(step, **kwargs):
        h.controller.pan_calls.append(step)
        return {"settled": True, "fov_x": 0., "limited": True}
    monkeypatch.setattr(h.controller, "follow_horizontal", limited)
    for x in (.42, .46, .50, .54, .557):
        h.feed([boat(x, reverse=reverse)], advance=.5); h.drain_control()
    assert h.manager._pan_limited
    count = len(h.controller.pan_calls)
    acquire(h, .58, reverse=reverse)
    assert len(h.controller.pan_calls) == count and h.controller.calls == [1, 3]
    h.depart(); h.phase("waiting_body")
    assert h.manager.active["id"] == rid and not h.manager.active.get("media_stopped")
    assert h.manager._pan_offset == 0
    acquire(h, .60, reverse=reverse)  # tail before overview exit line -> repeat
    h.phase("zoomed")
    assert h.controller.calls == [1, 3, 1, 3]
    assert h.manager.active["id"] == rid
    acquire(h, .62, reverse=reverse)
    h.depart(); h.phase("waiting_body")
    acquire(h, .70, reverse=reverse)  # tail beyond overview exit -> no repeat
    assert h.manager._patrol_done and h.controller.calls == [1, 3, 1, 3, 1]
    acquire(h, .85, reverse=reverse)
    assert h.manager.active["id"] == rid
    for _ in range(6): h.feed([], advance=.2)
    h.phase("overview")
    h.manager.executor.submit(lambda: None).result(timeout=5)
    assert len(h.manager.catalog) == 1 and h.manager.catalog[rid]["status"] == "completed"
    assert h.manager.catalog[rid]["patrol_events"][-1]["event"] == "overview_stern_left"
    if not reverse:
        capture = cv2.VideoCapture(str(h.manager.root / rid / "cycle.mp4"))
        try:
            assert capture.isOpened() and capture.get(cv2.CAP_PROP_FRAME_COUNT) > 50
            assert capture.read()[0]
        finally:
            capture.release()


def test_configured_overview_gate_and_unknown_direction_do_not_guess(cycle_factory):
    h = patrol(cycle_factory, overview_line_left=.45, overview_line_right=.8)
    h.warm()
    for _ in range(4): h.feed([boat(.1, width=.3)], advance=.3)
    assert h.manager._patrol_direction == 0 and h.controller.calls == [1]
    h.feed([boat(.12, width=.3)])
    assert h.manager._patrol_direction == 1 and h.controller.calls == [1]
    cross_entry(h, x=.17, width=.3)
    assert h.controller.calls == [1, 3]


def test_empty_zoomed_view_waits_for_entry_and_never_completes_pass_early(cycle_factory):
    h = patrol(cycle_factory, target_acquire_timeout_seconds=5)
    h.warm(); h.feed([boat(-.25)]); cross_entry(h)
    rid = h.manager.active["id"]
    for _ in range(5): h.feed([], advance=.5)
    assert h.controller.calls == [1, 3] and h.manager.active["id"] == rid
    for _ in range(7): h.feed([], advance=.5)
    h.phase("waiting_body")
    assert h.manager._patrol_done and h.manager.active["id"] == rid
    assert not h.manager.active.get("media_stopped")


def test_stale_empty_detection_cannot_return_the_current_zoom(cycle_factory):
    h = patrol(cycle_factory)
    h.warm(); h.feed([boat(-.25)])
    old = h.manager.zoom_observation_context()
    cross_entry(h); acquire(h, .38)
    for _ in range(10): h.feed([], context=old, advance=1)
    assert h.controller.calls == [1, 3]


@pytest.mark.parametrize("key,value", [("overview_line_left", 0), ("overview_line_right", 1),
    ("detail_line_left", .9), ("overview_line_left", .8), ("pan_max_left_travel_ratio", -.1)])
def test_invalid_line_and_directional_range_settings_are_rejected(key, value):
    with pytest.raises(ValidationError): RecordingSettings(**{key: value})


def test_removing_vertical_boundary_limit_uses_height_and_calibrated_six_x():
    target = {"height_ratio": .162, "box": [.05, .70, .4, .862]}
    factors = [1, 2, 3, 4, 6, 8]
    limited = VesselTargetSelector.plan_zoom(target, RecordingSettings(), factors)
    plan = VesselTargetSelector.plan_zoom(target, RecordingSettings(zoom_view_limits_enabled=False), factors)
    assert limited["zoom_factor"] == 1
    assert plan["requested_zoom_factor"] == pytest.approx(1 / .162)
    assert plan["zoom_factor"] == 6 and plan["selection_reason"] == "adaptive_calibrated"


def test_single_edge_box_jitter_cannot_reverse_direction_and_zoom_before_entry(cycle_factory):
    h = patrol(cycle_factory)
    h.warm()
    h.feed([boat(.10, width=.20)])
    # The bow remains at x=.30, before the left entry line. Only the rear
    # edge jitters, shifting the box centre left without a right-to-left ship.
    for _ in range(3):
        h.feed([boat(.075, width=.225)], advance=.5)
        h.drain_control()
    assert h.controller.calls == [1] and h.manager.phase == "waiting_body"


@pytest.mark.parametrize("reverse", [False, True])
def test_separated_entry_line_outliers_do_not_accumulate_as_continuous_crossing(cycle_factory, reverse):
    h = patrol(cycle_factory, overview_settle_seconds=.4)
    h.warm()
    h.feed([boat(-.25, reverse=reverse)])
    h.feed([boat(-.15, reverse=reverse)], advance=.1)
    h.feed([boat(-.20, reverse=reverse)], advance=.5)
    h.feed([boat(-.15, reverse=reverse)], advance=.1)
    h.drain_control()
    assert h.controller.calls == [1] and h.manager.phase == "waiting_body"


def test_repeated_delivery_of_one_source_frame_cannot_confirm_entry(cycle_factory):
    h = patrol(cycle_factory)
    h.warm(); h.feed([boat(-.25)])
    context = {**h.manager.zoom_observation_context(), "source_timestamp": h.timestamp}
    for _ in range(4): h.feed([boat(-.15)], context=context, advance=.5)
    assert h.controller.calls == [1] and h.manager._patrol_entry_hits == 1
    context["source_timestamp"] = h.timestamp
    h.feed([boat(-.15)], context=context)
    h.phase("zoomed")
    event = h.manager.catalog[h.manager.active["id"]]["patrol_events"][-1]
    assert event["entry_confirm_hits"] == 2 and event["bow"] >= event["entry_line"]


def test_manual_lens_reservation_defers_automatic_initialization(cycle_factory):
    h = patrol(cycle_factory)
    generation = h.manager.begin_manual_zoom()
    h.feed([boat(-.25)]); h.drain_control()
    assert not h.controller.calls
    h.manager.end_manual_zoom(3, generation)
    h.feed([boat(-.25)]); h.phase("overview")
    assert h.controller.calls == [1]


@pytest.mark.parametrize("reverse", [False, True])
def test_configurable_detail_lines_use_enlarged_x_coordinates(cycle_factory, reverse, monkeypatch):
    h = patrol(cycle_factory, detail_line_left=.2, detail_line_right=.8)
    h.warm(); h.feed([boat(-.25, reverse=reverse)]); cross_entry(h, reverse=reverse)
    acquire(h, .38, reverse=reverse)
    def limited(step, **kwargs):
        h.controller.pan_calls.append(step)
        return {"settled": True, "fov_x": 0., "limited": True}
    monkeypatch.setattr(h.controller, "follow_horizontal", limited)
    for x in (.38, .39):
        h.feed([boat(x, reverse=reverse)], advance=.4); h.drain_control()
    # Reference x=.39 becomes detail x=.17, still before the first (.2) line.
    assert not h.controller.pan_calls
    h.feed([boat(.405, reverse=reverse)], advance=.4); h.drain_control()
    assert h.controller.pan_calls and (h.controller.pan_calls[0] < 0) == reverse


def test_two_overlapping_arrivals_cannot_claim_the_locked_ship(cycle_factory):
    h = patrol(cycle_factory)
    h.warm(); h.feed([boat(-.25)]); cross_entry(h)
    # Neither hull is the locked upstream identity; both are equally plausible.
    for _ in range(8): h.feed([boat(.35, ship_id=98), boat(.36, ship_id=99)], advance=.2)
    assert h.manager.locked_target["ship_id"] == 1
    assert not h.manager._zoomed_ship_seen and not h.controller.pan_calls


def test_stop_during_repeat_zoom_keeps_return_order_and_closes_same_file(cycle_factory):
    h = patrol(cycle_factory)
    h.warm(); h.feed([boat(-.25)]); cross_entry(h)
    acquire(h, .38); h.depart(); h.phase("waiting_body")
    rid = h.manager.active["id"]
    h.controller.block_factor = 3
    for _ in range(4): h.feed([boat(.6)], advance=.2)
    assert h.controller.entered.wait(1)
    h.manager.finish()
    h.controller.release.set()
    assert h.manager.wait_for_optical_stop(timeout=5)
    assert h.manager.current_zoom_factor == 1 and h.manager._pan_offset == 0
    assert h.manager.active is None and len(h.manager.catalog) == 1
    assert h.controller.calls[-2:] == [3, 1]
    assert h.manager.catalog[rid]["returned_to_overview"]
