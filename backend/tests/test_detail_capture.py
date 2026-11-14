"""Detail collection keeps the enlarged lens until fresh departure evidence."""
from test_optical_cycle import cycle_factory, ship
from test_following_cycle import hull
from test_adaptive_optical import harness_factory, ship as adaptive_ship, projected


def test_detail_mode_limits_pre_alignment_then_follows_after_zoom(cycle_factory):
    h = cycle_factory(detail_capture_priority=True, wait_full_body=True, auto_horizontal_follow=True)
    h.warm()
    for _ in range(3): h.feed([hull(.64, .2)], advance=.5)
    h.drain_control()
    assert len(h.controller.pan_calls) == 1 and h.controller.calls == [1]
    for _ in range(2): h.feed([hull(.64, .2)], advance=.5)
    h.phase("zoomed")
    assert h.controller.calls == [1, 3]
    h.feed([hull(.64, .2)], advance=3)
    h.drain_control()
    pans = [i for i, event in enumerate(h.events) if event[0] == "pan"]
    assert len(pans) == 2 and pans[0] < h.events.index(("move_settled", 3)) < pans[1]


def test_entering_narrow_bow_does_not_start_pan_following_before_zoom(cycle_factory):
    h = cycle_factory(detail_capture_priority=True, wait_full_body=True, auto_horizontal_follow=True)
    h.warm()
    for _ in range(8): h.feed([hull(-.02, .2)], advance=.5)
    assert h.manager.phase == "waiting_body" and not h.controller.pan_calls


def test_failed_centering_does_not_continue_one_x_pan_indefinitely(cycle_factory, monkeypatch):
    h = cycle_factory(detail_capture_priority=True, wait_full_body=True, auto_horizontal_follow=True)
    h.warm()
    def stationary_pan(step, **kwargs):
        h.controller.pan_calls.append(step)
        return {"settled": True, "fov_x": 0., "limited": False}
    monkeypatch.setattr(h.controller, "follow_horizontal", stationary_pan)
    for _ in range(14):
        h.feed([hull(.64, .2)], advance=1)
        h.drain_control()
    assert h.controller.calls == [1] and len(h.controller.pan_calls) == 1


def test_pan_range_limit_holds_zoom_while_ship_is_visible_then_returns(cycle_factory, monkeypatch):
    h = cycle_factory(detail_capture_priority=True, auto_horizontal_follow=True)
    h.warm()
    def limited(step, **kwargs):
        h.controller.pan_calls.append(step)
        return {"settled": True, "fov_x": 0., "limited": True}
    monkeypatch.setattr(h.controller, "follow_horizontal", limited)
    h.feed([hull(.6, .2)]); h.phase("zoomed")
    for _ in range(20):
        h.feed([hull(.6, .2)], advance=1)
        h.drain_control()
    rid = h.manager.active["id"]
    assert h.manager._pan_limited and len(h.controller.pan_calls) == 1
    assert h.controller.calls == [1, 3]
    assert h.manager.status()["zoom_locked_until_departure"]
    assert h.manager.catalog[rid]["horizontal_limit_reached"]
    h.depart(); h.phase("overview")
    assert h.controller.calls == [1, 3, 1] and h.manager._pan_offset == 0


def test_detail_mode_keeps_magnification_even_when_height_changes(harness_factory):
    h = harness_factory(detail_capture_priority=True, zoom_adjust_cooldown_seconds=1)
    h.warm(); h.start([adaptive_ship()])
    for _ in range(15): h.feed([projected(adaptive_ship(height=.35), 3)], advance=1)
    assert h.lens.calls == [1, 3]
    assert h.manager.current_zoom_factor == 3


def test_visible_ambiguous_hull_does_not_trigger_identity_timeout_zoom_out(cycle_factory, monkeypatch):
    h = cycle_factory(detail_capture_priority=True, target_acquire_timeout_seconds=3)
    h.warm(); rid = h.start_cycle()
    def uncertain(timestamp):
        h.manager._association_uncertain = True
        return None
    monkeypatch.setattr(h.manager, "_find_locked_target", uncertain)
    for _ in range(10): h.feed([ship()], advance=1)
    assert h.controller.calls == [1, 3] and h.manager.active["id"] == rid
    assert h.manager._cycle_exit_reason == "vessel_left"
    h.depart(); h.phase("overview")
    assert h.controller.calls == [1, 3, 1]


def test_detail_mode_stop_still_returns_home_without_waiting_for_departure(cycle_factory):
    h = cycle_factory(detail_capture_priority=True, auto_horizontal_follow=True)
    h.warm(); h.start_cycle()
    h.manager.finish()
    assert h.manager.wait_for_optical_stop(timeout=5)
    assert h.manager.current_zoom_factor == 1 and h.manager._pan_offset == 0
