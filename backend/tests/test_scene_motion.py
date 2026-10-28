import cv2
import numpy as np
import pytest

from app.scene_motion import BackgroundMotionEstimator


def textured_background():
    rng = np.random.default_rng(37)
    image = np.zeros((240, 480, 3), dtype=np.uint8)
    for x, y, value in rng.integers([10, 10, 60], [465, 140, 250], size=(160, 3)):
        cv2.rectangle(image, (int(x), int(y)), (int(x) + 5, int(y) + 5), (int(value),) * 3, -1)
    return image


def test_stationary_textured_view_reports_verified_zero_motion():
    image = textured_background()
    estimator = BackgroundMotionEstimator()
    assert estimator.update(image, [], 0)["valid"] is False
    motion = estimator.update(image, [], .25)
    assert motion["valid"] is True
    assert motion["dx"] == pytest.approx(0, abs=.001)
    assert motion["dy"] == pytest.approx(0, abs=.001)
    assert motion["scale"] == pytest.approx(1, abs=.001)


def test_background_translation_is_separate_from_detected_ship_motion():
    background = textured_background()
    first = background.copy()
    second = cv2.warpAffine(background, np.float32([[1, 0, 8], [0, 1, 3]]), (480, 240))
    rng = np.random.default_rng(18)
    foreground = rng.integers(0, 255, (60, 150, 3), dtype=np.uint8)
    first[90:150, 90:240] = foreground
    second[90:150, 170:320] = foreground
    estimator = BackgroundMotionEstimator()
    estimator.update(first, [{"boxes": [{"type": "ship", "xywh": [90, 90, 150, 60]}]}], 0)
    motion = estimator.update(second, [{"boxes": [{"type": "ship", "xywh": [170, 90, 150, 60]}]}], .25)
    assert motion["valid"] is True
    assert motion["dx"] == pytest.approx(8 / 480, abs=.003)
    assert motion["dy"] == pytest.approx(3 / 240, abs=.003)


def test_lens_generation_change_discards_previous_motion_reference():
    image = textured_background()
    estimator = BackgroundMotionEstimator()
    estimator.update(image, [], 0, generation=1)
    assert estimator.update(image, [], .25, generation=1)["valid"] is True
    changed = estimator.update(image, [], .5, generation=2)
    assert changed["valid"] is False
    assert changed["reason"] == "new_view"
    assert estimator.update(image, [], .75, generation=2)["valid"] is True


def test_featureless_view_and_observation_gap_are_not_proof_of_stationarity():
    estimator = BackgroundMotionEstimator()
    empty = np.zeros((240, 480, 3), dtype=np.uint8)
    estimator.update(empty, [], 0)
    assert estimator.update(empty, [], .25)["valid"] is False
    image = textured_background()
    estimator.update(image, [], .5)
    gap = estimator.update(image, [], 5)
    assert gap["valid"] is False
    assert gap["reason"] == "observation_gap"


def test_large_pan_is_reported_unstable_instead_of_accumulating_stationary_evidence():
    image = textured_background()
    estimator = BackgroundMotionEstimator()
    estimator.update(image, [], 0)
    moved = cv2.warpAffine(image, np.float32([[1, 0, 80], [0, 1, 0]]), (480, 240))
    result = estimator.update(moved, [], .25)
    assert result["valid"] is False
