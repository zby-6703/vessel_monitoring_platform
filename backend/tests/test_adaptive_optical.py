"""Selected-vessel lifecycle must not be held open by another ship in view."""
from __future__ import annotations

import numpy as np
import pytest

from app import recording as recording_module
from app import optical_recording as optical_module
from app.recording import RecordingManager


WIDTH, HEIGHT = 1000, 600


def ship(ship_id=1, *, x=.4, y=.4, width=.1, height=.2, name="目标船"):
    return {"ship_id": ship_id, "ship_name": name, "confidence": .95,
            "boxes": [{"type": "ship", "confidence": .95,
                       "xywh": [x * WIDTH, y * HEIGHT, width * WIDTH, height * HEIGHT]}]}


def projected(vessel, factor):
    value = {**vessel, "boxes": []}
    for box in vessel["boxes"]:
        x, y, width, height = box["xywh"]
        value["boxes"].append({**box, "xywh": [
            ((x / WIDTH - .5) * factor + .5) * WIDTH,
            ((y / HEIGHT - .5) * factor + .5) * HEIGHT,
            width * factor, height * factor,
        ]})
    return value


class FakeLens:
    def __init__(self):
        self.calls = []

    def available_magnifications(self):
        return [1, 2, 3, 5, 8]

    def status(self):
        return {"magnifications": self.available_magnifications(), "cycle_supported": True}

    def validate_cycle(self, factor):
        if factor not in self.available_magnifications():
            raise ValueError("倍率未标定")

    def move_to(self, factor):
        self.validate_cycle(factor)
        self.calls.append(factor)
        return {"factor": factor, "position": factor / 10, "settled": True}


class Harness:
    def __init__(self, manager, lens):
        self.manager, self.lens = manager, lens
        self.timestamp = 0.
        self.image = np.full((HEIGHT, WIDTH, 3), 100, np.uint8)

    def feed(self, vessels=None, *, advance=.1):
        self.timestamp += advance
        context = self.manager.zoom_observation_context() if vessels is not None else None
        self.manager.process_frame(self.image, camera_id="adaptive-camera", session_id="adaptive-session",
                                   source_name="自适应目标测试", source_type="camera", fps=10,
                                   timestamp=self.timestamp, vessels=vessels, observation_context=context)
        return self.manager.status()

    def settle(self, phase):
        for _ in range(30):
            self.manager.control_executor.submit(lambda: None).result(timeout=5)
            state = self.feed()
            if state["phase"] == phase:
                return state
        raise AssertionError(f"never reached {phase}: {self.manager.status()}")

    def warm(self):
        self.feed()
        self.settle("overview")

    def start(self, vessels):
        state = self.feed(vessels)
        assert state["active_id"], state
        self.settle("zoomed")
        return self.manager.status()["active_id"]

    def depart_with(self, remaining):
        self.feed(remaining)
        for _ in range(10):
            state = self.feed(remaining)
            if state["phase"] != "zoomed":
                return self.settle("overview")
        raise AssertionError(f"remaining non-target vessel kept recording alive: {self.manager.status()}")


@pytest.fixture
def harness_factory(tmp_path, monkeypatch):
    created = []

    class SpyWriter:
        def __init__(self, *_args, **_kwargs): pass
        def write(self, *_args): pass
        def close(self): pass

    class StaticBackground:
        """Geometry tests provide known static scenery, not featureless video."""
        def update(self, *_args, **_kwargs):
            return {"dx": 0, "dy": 0, "scale": 1, "valid": True, "elapsed_seconds": .1}

        def reset(self): pass

    monkeypatch.setattr(recording_module, "ClipWriter", SpyWriter)
    monkeypatch.setattr(optical_module, "BackgroundMotionEstimator", StaticBackground)

    def make(**values):
        manager = RecordingManager(tmp_path / str(len(created)))
        lens = FakeLens()
        manager.set_zoom_controller(lens)
        def finalize(recording_id, status):
            with manager.lock:
                manager.catalog[recording_id]["status"] = status
        monkeypatch.setattr(manager, "_encode", finalize)
        config = {"enabled": True, "recording_mode": "optical_cycle", "zoom_factor": 3,
                  "adaptive_zoom_enabled": True, "target_height_ratio": .6,
                  "min_optical_zoom": 1.1, "max_optical_zoom": 8,
                  "suppress_stationary": False, "min_hits": 1, "pre_roll_seconds": 0,
                  "overview_settle_seconds": 0, "departure_confirm_seconds": .3}
        config.update(values)
        manager.update_settings(config)
        harness = Harness(manager, lens)
        created.append(harness)
        return harness

    yield make
    for harness in created:
        harness.manager.finish()
        harness.manager.wait_for_optical_stop(timeout=5)
        harness.manager.control_executor.shutdown(wait=True)
        harness.manager.executor.shutdown(wait=True)


def test_selected_ship_leaving_ends_cycle_even_while_another_ship_is_visible(harness_factory):
    harness = harness_factory()
    harness.warm()
    target = ship()
    other = ship(2, x=.6, y=.44, height=.12, name="旁边停泊船")
    recording_id = harness.start([target, other])
    assert harness.lens.calls[-1] == 3
    harness.feed([projected(target, 3), projected(other, 3)])
    harness.depart_with([projected(other, 3)])
    harness.manager.executor.submit(lambda: None).result(timeout=5)
    item = harness.manager.catalog[recording_id]
    assert item["status"] == "completed"
    assert item["ship_names"] == ["目标船"]
    assert item["track_ids"] == [1]
    assert item["returned_to_overview"] is True


def test_larger_new_ship_does_not_switch_the_locked_target_mid_cycle(harness_factory):
    harness = harness_factory()
    harness.warm()
    target = ship()
    recording_id = harness.start([target])
    intruder = ship(3, x=.7, y=.25, width=.1, height=.5, name="新进入大船")
    harness.feed([projected(target, 3), intruder])
    assert harness.manager.status()["active_id"] == recording_id
    assert harness.lens.calls == [1, 3]
    assert harness.manager.catalog[recording_id]["ship_names"] == ["目标船"]


def test_one_x_is_a_recording_cycle_not_an_immediate_return(harness_factory):
    harness = harness_factory()
    harness.warm()
    target = ship(x=.4, y=.15, height=.7)
    recording_id = harness.start([target])
    assert harness.manager.status()["current_zoom_factor"] == 1
    assert harness.manager.status()["phase"] == "zoomed"
    harness.feed([target])
    assert harness.manager.status()["active_id"] == recording_id
    assert all(factor == 1 for factor in harness.lens.calls)
    harness.depart_with([])
    assert harness.manager.status()["active_id"] is None


def test_a_moving_far_ship_can_trigger_while_near_ship_is_parked(harness_factory):
    harness = harness_factory(suppress_stationary=True, stationary_seconds=3,
                              motion_confirm_seconds=.5, stationary_motion_threshold=.015)
    harness.warm()
    parked = ship(8, x=.05, y=.3, height=.4, name="停泊船")
    for _ in range(9):
        assert harness.feed([parked], advance=.5)["active_id"] is None
    state = None
    for index in range(8):
        passer = ship(9, x=.4 + index * .02, y=.4, height=.2, name="经过船")
        state = harness.feed([parked, passer], advance=.5)
        if state["active_id"]:
            break
    assert state["active_id"], state
    item = harness.manager.catalog[state["active_id"]]
    assert item["ship_names"] == ["经过船"]
    assert item["track_ids"] == [9]


def test_approaching_locked_ship_reduces_zoom_after_cooldown_without_splitting_video(harness_factory):
    harness = harness_factory(zoom_adjust_cooldown_seconds=1)
    harness.warm()
    target = ship()
    recording_id = harness.start([target])
    harness.feed([projected(target, 3)])
    nearer = ship(y=.35, height=.3)
    for _ in range(6):
        factor = harness.manager.status()["current_zoom_factor"] or harness.lens.calls[-1]
        harness.feed([projected(nearer, factor)], advance=.5)
        harness.settle("zoomed")
        if harness.lens.calls[-1] == 2:
            break
    assert harness.lens.calls == [1, 3, 2]
    assert harness.manager.status()["active_id"] == recording_id
    item = harness.manager.catalog[recording_id]
    assert item["initial_optical_zoom_factor"] == 3
    assert item["optical_zoom_factor"] == 2
    assert item["ship_names"] == ["目标船"]
    assert len(harness.manager.catalog) == 1
