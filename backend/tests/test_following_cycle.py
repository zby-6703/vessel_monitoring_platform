import threading

import pytest

from test_optical_cycle import cycle_factory, ship


def hull(x, width=.3):
    return {"ship_id": 1, "ship_name": "TEST", "confidence": .95,
            "boxes": [{"type": "ship", "xywh": [x * 64, 20, width * 64, 8]}]}


def test_partial_bow_is_recorded_at_one_x_until_full_hull_is_stable(cycle_factory):
    h = cycle_factory(wait_full_body=True, body_entry_confirm_seconds=.4)
    h.warm()
    for _ in range(6):
        state = h.feed([hull(-.02, .96)], advance=.2)
    rid = state["active_id"]
    assert rid and state["phase"] == "waiting_body"
    assert h.controller.calls == [1]
    for _ in range(4): h.feed([hull(.04, .9)], advance=.2)
    h.phase("zoomed")
    assert h.controller.calls == [1, 3]
    assert h.manager.active["id"] == rid
    assert h.manager.catalog[rid]["waiting_full_body"] is False


def test_a_hull_wider_than_the_view_keeps_full_view_recording(cycle_factory):
    h = cycle_factory(wait_full_body=True)
    h.warm()
    for _ in range(10): state = h.feed([hull(-.2, 1.4)], advance=1)
    assert state["active_id"] and state["phase"] == "waiting_body"
    assert h.controller.calls == [1]


def test_transient_full_width_detection_does_not_satisfy_body_admission(cycle_factory):
    h = cycle_factory(wait_full_body=True, body_entry_confirm_seconds=.5)
    h.warm()
    h.feed([hull(-.1)])
    for _ in range(3):
        h.feed([hull(.2)], advance=.1)
        h.feed([hull(-.1)], advance=.1)
    assert h.manager.phase == "waiting_body" and h.controller.calls == [1]


def test_horizontal_follow_keeps_identity_in_the_reference_plane_and_returns_home(cycle_factory):
    h = cycle_factory(auto_horizontal_follow=True, pan_cooldown_seconds=.5)
    h.warm()
    target = hull(.6, .2)
    h.feed([target])
    h.phase("zoomed")
    h.feed([target], advance=.2)
    h.drain_control()
    assert h.controller.pan_calls and all(step > 0 for step in h.controller.pan_calls)
    assert h.manager._pan_offset > 0
    rid = h.manager.active["id"]
    locked = h.manager.locked_target["target_id"]
    for _ in range(3): h.feed([target], advance=.2)
    assert h.manager.active["id"] == rid
    assert h.manager.locked_target["target_id"] == locked
    assert h.manager._media_last_target == pytest.approx(h.timestamp)
    h.manager.finish()
    h.drain_control()
    assert h.manager.current_zoom_factor == 1
    assert h.manager._pan_offset == 0
    assert h.controller.pan_returns >= 2


def test_center_dead_zone_does_not_move_the_camera(cycle_factory):
    h = cycle_factory(auto_horizontal_follow=True)
    h.warm(); h.start_cycle()
    for _ in range(10): h.feed([ship()], advance=.3)
    assert not h.controller.pan_calls


def test_stop_during_horizontal_move_waits_for_return_without_new_source_frames(cycle_factory):
    h = cycle_factory(auto_horizontal_follow=True)
    h.warm()
    h.controller.block_pan = True
    h.feed([hull(.6, .2)]); h.phase("zoomed")
    h.feed([hull(.6, .2)])
    assert h.controller.entered.wait(1)
    assert h.manager.phase == "panning"
    h.manager.finish()
    h.controller.release.set()
    assert h.manager.wait_for_optical_stop(timeout=5)
    assert h.manager.current_zoom_factor == 1
    assert h.manager._pan_offset == 0 and h.manager._optical_session is False


def test_zoom_hold_and_longer_empty_confirmation_prevent_immediate_shrink(cycle_factory):
    h = cycle_factory(zoom_min_hold_seconds=12, departure_confirm_seconds=5, departure_min_observations=3)
    h.warm(); rid = h.start_cycle()
    for _ in range(10): h.feed([], advance=1)
    assert not h.manager._media_pending
    assert h.controller.calls == [1, 3] and h.manager.active["id"] == rid
    h.feed([ship()], advance=1)
    for _ in range(3): h.feed([], advance=.5)
    assert h.controller.calls == [1, 3]
    for _ in range(7): h.feed([], advance=1)
    h.phase("overview")
    assert h.controller.calls == [1, 3, 1]


def test_sparse_empty_observations_require_a_minimum_count(cycle_factory):
    h = cycle_factory(departure_confirm_seconds=1, departure_min_observations=3)
    h.warm(); h.start_cycle()
    h.feed([], advance=2); h.feed([], advance=2)
    assert h.manager.phase == "zoomed"
    h.feed([], advance=2)
    h.phase("overview")
    assert h.controller.calls == [1, 3, 1]


def test_stale_pre_pan_detections_cannot_end_the_new_view(cycle_factory):
    h = cycle_factory(auto_horizontal_follow=True)
    h.warm()
    target = hull(.6, .2)
    h.feed([target]); h.phase("zoomed")
    old_context = h.manager.zoom_observation_context()
    h.feed([target]); h.drain_control()
    rid = h.manager.active["id"]
    for _ in range(8): h.feed([], context=old_context, advance=1)
    assert h.manager.active["id"] == rid
    assert h.manager.phase == "zoomed"


def test_zoom_hold_keeps_visible_ship_recording_even_when_motion_is_stationary(cycle_factory):
    h = cycle_factory(zoom_min_hold_seconds=12)
    h.warm(); rid = h.start_cycle()
    h.manager._cycle_policy.suppress_stationary = True
    h.manager._cycle_policy.stationary_seconds = 3
    for _ in range(20): h.feed([ship()], advance=1)
    assert h.manager.active["id"] == rid and h.manager.phase == "zoomed"


def test_transport_failure_recovers_by_absolute_home_without_repeating_pan(cycle_factory, monkeypatch):
    import httpx
    h = cycle_factory(auto_horizontal_follow=True)
    h.warm()
    calls = []
    def failed_pan(*args, **kwargs):
        calls.append(args)
        raise httpx.ReadTimeout("lost pan reply")
    monkeypatch.setattr(h.controller, "follow_horizontal", failed_pan)
    target = hull(.6, .2)
    h.feed([target]); h.phase("zoomed")
    h.feed([target]); h.drain_control()
    assert h.manager.phase == "recovering"
    rid = h.manager.active["id"]
    assert h.manager.catalog[rid]["pan_errors"][0]["kind"] == "ReadTimeout"
    h.manager._zoom_recovery_due = 0
    h.feed(None)
    h.phase("zoomed")
    for _ in range(12): h.feed([target], advance=.2)
    assert len(calls) == 1
    assert h.manager.error is None
    assert h.manager._pan_offset == 0 and h.manager._zoom_fallback_active
    h.depart(); h.phase("overview")
    h.manager.executor.submit(lambda: None).result(timeout=5)
    assert h.manager.catalog[rid]["status"] == "interrupted"
