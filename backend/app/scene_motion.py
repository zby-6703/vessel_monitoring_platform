"""Estimate camera/background motion separately from detected vessel motion.

This is a visual stabilization heuristic, not a calibrated camera pose sensor.
Insufficient background texture explicitly produces ``valid=False`` so callers
must not accumulate stationary-vessel evidence from an unverified view.
"""
from __future__ import annotations

import math

import cv2
import numpy as np


class BackgroundMotionEstimator:
    def __init__(self, max_width: int = 640, min_inliers: int = 12):
        self.max_width = max_width
        self.min_inliers = min_inliers
        self.reset()

    def reset(self):
        self._gray = None
        self._mask = None
        self._timestamp = None
        self._generation = None

    def _prepare(self, image, vessels):
        source_h, source_w = image.shape[:2]
        width = min(self.max_width, source_w)
        height = max(2, round(source_h * width / source_w))
        gray = cv2.cvtColor(cv2.resize(image, (width, height)), cv2.COLOR_BGR2GRAY)
        mask = np.full((height, width), 255, dtype=np.uint8)
        # Most lower-water features belong to waves/reflections, not a rigid
        # background. Ignore them rather than treating their drift as camera pan.
        mask[round(height * .62):] = 0
        mask[:max(2, round(height * .025))] = 0
        for vessel in vessels or []:
            for box in vessel.get("boxes", []):
                if box.get("type") != "ship":
                    continue
                values = box.get("xywh", [])
                if len(values) != 4 or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
                    continue
                x, y, w, h = values
                if w <= 0 or h <= 0:
                    continue
                pad = max(8, round(max(w, h) * width / source_w * .04))
                x1, y1 = min(width, max(0, round(x * width / source_w) - pad)), min(height, max(0, round(y * height / source_h) - pad))
                x2, y2 = min(width, max(0, round((x + w) * width / source_w) + pad)), min(height, max(0, round((y + h) * height / source_h) + pad))
                if x2 > x1 and y2 > y1:
                    mask[y1:y2, x1:x2] = 0
        return gray, mask

    def update(self, image, vessels, timestamp, *, generation=0):
        if image is None or image.ndim != 3 or min(image.shape[:2]) < 2 or not math.isfinite(timestamp):
            self.reset()
            return self._invalid("invalid_frame")
        gray, mask = self._prepare(image, vessels)
        previous_gray, previous_mask, previous_time = self._gray, self._mask, self._timestamp
        changed = self._generation != generation
        self._gray, self._mask, self._timestamp, self._generation = gray, mask, timestamp, generation
        if previous_gray is None or changed or previous_gray.shape != gray.shape:
            return self._invalid("new_view")
        elapsed = timestamp - previous_time
        if not 0 < elapsed <= 3:
            return self._invalid("observation_gap", elapsed)
        points = cv2.goodFeaturesToTrack(previous_gray, maxCorners=500, qualityLevel=.015, minDistance=7,
                                        mask=previous_mask, blockSize=5)
        if points is None or len(points) < self.min_inliers:
            return self._invalid("insufficient_background_features", elapsed)
        tracked, status, errors = cv2.calcOpticalFlowPyrLK(previous_gray, gray, points, None,
            winSize=(25, 25), maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, .01))
        if tracked is None or status is None:
            return self._invalid("tracking_failed", elapsed)
        before, after = points.reshape(-1, 2), tracked.reshape(-1, 2)
        h, w = gray.shape
        valid = status.reshape(-1).astype(bool)
        valid &= np.all(np.isfinite(after), axis=1)
        if errors is not None:
            valid &= errors.reshape(-1) < 24
        valid &= (after[:, 0] >= 0) & (after[:, 0] < w) & (after[:, 1] >= 0) & (after[:, 1] < h)
        indices = np.flatnonzero(valid)
        indices = np.array([i for i in indices if mask[int(after[i, 1]), int(after[i, 0])] > 0], dtype=int)
        if len(indices) < self.min_inliers:
            return self._invalid("insufficient_background_matches", elapsed)
        before, after = before[indices], after[indices]
        affine, inliers = cv2.estimateAffinePartial2D(before, after, method=cv2.RANSAC,
            ransacReprojThreshold=1.8, maxIters=2000, confidence=.99, refineIters=10)
        if affine is None or inliers is None:
            return self._invalid("background_fit_failed", elapsed)
        inlier_mask = inliers.reshape(-1).astype(bool)
        count, ratio = int(inlier_mask.sum()), float(inlier_mask.mean())
        if count < self.min_inliers or ratio < .55:
            return self._invalid("background_fit_unreliable", elapsed)
        spread = np.ptp(before[inlier_mask], axis=0)
        if spread[0] < w * .15 or spread[1] < h * .035:
            return self._invalid("background_features_too_local", elapsed)
        center = np.array([w / 2, h / 2])
        shift = affine[:, :2] @ center + affine[:, 2] - center
        dx, dy = float(shift[0] / w), float(shift[1] / h)
        scale = float(math.hypot(affine[0, 0], affine[1, 0]))
        rotation = math.degrees(math.atan2(affine[1, 0], affine[0, 0]))
        if not np.all(np.isfinite(affine)) or not .96 <= scale <= 1.04 or abs(rotation) > 3 or abs(dx) > .08 or abs(dy) > .08:
            return self._invalid("unstable_camera_view", elapsed)
        residual = float(np.median(np.linalg.norm((before @ affine[:, :2].T + affine[:, 2]) - after, axis=1)))
        return {"dx": dx, "dy": dy, "scale": scale, "valid": True, "elapsed_seconds": elapsed,
                "inliers": count, "inlier_ratio": round(ratio, 4), "residual_pixels": round(residual, 4),
                "rotation_degrees": rotation, "reason": None}

    @staticmethod
    def _invalid(reason, elapsed=0):
        return {"dx": 0., "dy": 0., "scale": 1., "valid": False,
                "elapsed_seconds": max(0., elapsed), "reason": reason}
