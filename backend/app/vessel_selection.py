"""Image-space vessel scheduling, motion evidence and calibrated zoom planning.

Apparent hull height is a proximity heuristic, never a metric distance. Tracks
live in the wide-view coordinate plane and do not trust upstream IDs alone.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from statistics import median

import cv2
import numpy as np


def _iou(a, b):
    overlap = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    return overlap / max(1e-9, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap)


def _center(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


@dataclass
class _Target:
    key: int
    ship_id: int
    box: tuple
    last_seen: float
    anchor: tuple
    motion_anchor: tuple
    still_seconds: float = 0.
    moving_seconds: float = 0.
    state: str = "observing"
    heights: deque = field(default_factory=lambda: deque(maxlen=5))
    history: deque = field(default_factory=lambda: deque(maxlen=24))
    vessel: dict = field(default_factory=dict)
    appearance: tuple | None = None
    reference_height: float = 0.
    stable_height: float = 0.
    last_moving_at: float | None = None


class VesselTargetSelector:
    MAX_TRACKS = 256
    MAX_OBSERVATION_GAP = 2.5

    def __init__(self):
        self.tracks: dict[int, _Target] = {}
        self.next_id = 1
        self.selected_id = None
        self.scale = 1.
        self.offset = [0., 0.]
        self.last_timestamp = None
        self.result = {"selected": None, "targets": [], "selected_target_id": None,
                       "zoom_factor": 1., "requested_zoom_factor": 1., "selection_reason": "no_moving_target"}

    @staticmethod
    def detection_box(vessel, width, height, zoom_factor=1.):
        try:
            confidence = float(vessel.get("confidence", 0))
            if not math.isfinite(confidence):
                return None
            for box in vessel.get("boxes", []):
                if box.get("type") != "ship":
                    continue
                x, y, w, h = [float(n) for n in box["xywh"]]
                if not all(math.isfinite(n) for n in (x, y, w, h)) or w <= 0 or h <= 0:
                    continue
                x1, y1 = max(0., x / width), max(0., y / height)
                x2, y2 = min(1., (x + w) / width), min(1., (y + h) / height)
                if x2 <= x1 or y2 <= y1:
                    continue
                return tuple(.5 + (n - .5) / zoom_factor for n in (x1, y1, x2, y2))
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            pass
        return None

    @staticmethod
    def plan_zoom(target, settings, calibrated_factors=None):
        height = max(.001, float(target["height_ratio"]))
        if not settings.adaptive_zoom_enabled:
            return {"zoom_factor": settings.zoom_factor, "requested_zoom_factor": settings.zoom_factor,
                    "selection_reason": "fixed_zoom", "max_safe_zoom": settings.zoom_factor}
        requested = settings.target_height_ratio / height
        box = target["box"]
        # Lens-only zoom is centred on the physical optical axis. A lower ship
        # must not silently disappear from the vertical field of view.
        edge = max(abs(box[1] - .5), abs(box[3] - .5), .001)
        # Keep waterline / deck detail off the image boundary. This margin is
        # expressed in the resulting image, so it remains consistent at every
        # calibrated magnification and does not depend on the current width of
        # a partially entering bow.
        safe = max(1., .47 / edge)
        limit = max(1., min(requested, settings.max_optical_zoom, safe))
        factors = sorted({float(f) for f in (calibrated_factors or [])
                          if isinstance(f, (int, float)) and math.isfinite(f) and f >= 1})
        usable = [f for f in factors if settings.min_optical_zoom <= f <= limit + 1e-8]
        zoom = max(usable, default=1.)
        reason = "adaptive_calibrated"
        if requested <= 1:
            reason = "already_large_enough"
        elif safe + .01 < min(requested, settings.max_optical_zoom):
            reason = "off_axis_limited"
        elif not usable:
            reason = "no_safe_calibrated_zoom"
        return {"zoom_factor": zoom, "requested_zoom_factor": requested,
                "selection_reason": reason, "max_safe_zoom": safe}

    def _stable_point(self, box):
        cx, cy = _center(box)
        return ((cx - self.offset[0]) / self.scale, (cy - self.offset[1]) / self.scale)

    @staticmethod
    def appearance_for(image, vessel):
        if image is None:
            return None
        height, width = image.shape[:2]
        box = VesselTargetSelector.detection_box(vessel, width, height)
        if box is None:
            return None
        x1, y1, x2, y2 = box
        crop = image[max(0, int(y1 * height)):max(1, int(y2 * height)), max(0, int(x1 * width)):max(1, int(x2 * width))]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(cv2.resize(crop, (64, 32)), cv2.COLOR_BGR2HSV)
        color = hsv[(hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 35), 0]
        if color.size < 32:
            return None
        histogram = np.histogram(color, bins=18, range=(0, 180))[0].astype(float)
        histogram = .6 * histogram + .2 * np.roll(histogram, 1) + .2 * np.roll(histogram, -1)
        histogram /= max(1e-9, np.linalg.norm(histogram))
        return tuple(histogram.tolist())

    @staticmethod
    def appearance_similarity(left, right):
        return float(np.dot(left, right)) if left is not None and right is not None else None

    def update(self, vessels, width, height, timestamp, settings, zoom_factor=1., calibrated_factors=None, camera_motion=None, image=None):
        if width <= 0 or height <= 0 or not math.isfinite(timestamp) or not math.isfinite(zoom_factor) or zoom_factor < 1:
            return dict(self.result)
        if self.last_timestamp is not None and timestamp < self.last_timestamp:
            self.__init__()
        previous = self.last_timestamp
        self.last_timestamp = timestamp
        dt = 0. if previous is None else timestamp - previous
        reliable = camera_motion is None or bool(camera_motion.get("valid"))
        dx = dy = 0.
        scale = 1.
        if reliable and camera_motion:
            dx, dy = float(camera_motion.get("dx", 0)) / zoom_factor, float(camera_motion.get("dy", 0)) / zoom_factor
            scale = float(camera_motion.get("scale", 1))
            if not all(math.isfinite(n) for n in (dx, dy, scale)) or not .8 <= scale <= 1.25:
                reliable, dx, dy, scale = False, 0., 0., 1.
            self.offset = [scale * self.offset[0] + dx + .5 * (1 - scale),
                           scale * self.offset[1] + dy + .5 * (1 - scale)]
            self.scale = max(.05, min(20., self.scale * scale))
        max_age = max(30., settings.stationary_seconds * 2)
        self.tracks = {key: track for key, track in self.tracks.items() if timestamp - track.last_seen <= max_age}
        parsed = []
        for vessel in vessels:
            box = self.detection_box(vessel, width, height, zoom_factor)
            confidence = float(vessel.get("confidence", 0)) if box else 0.
            if box and confidence >= settings.confidence_threshold:
                parsed.append((vessel, box, self.appearance_for(image, vessel)))
        # Global assignment by geometry prevents upstream track-ID churn from
        # turning the same moored hull into a fresh eligible target.
        pairs = []
        for index, (vessel, box, appearance) in enumerate(parsed):
            for key, track in self.tracks.items():
                predicted = tuple(.5 + (v - .5) * scale + (dx if axis % 2 == 0 else dy) for axis, v in enumerate(track.box))
                distance = math.dist(_center(box), _center(predicted))
                overlap = _iou(box, predicted)
                ratio = (box[3] - box[1]) / max(.001, predicted[3] - predicted[1])
                source_box = self.detection_box(vessel, width, height)
                clipped = source_box and (source_box[1] <= .003 or source_box[3] >= .997)
                if not clipped and box[3] - box[1] < track.reference_height * .4:
                    # Prevent a long sequence of plausible pairwise matches
                    # from migrating a foreground identity to a distant hull.
                    continue
                similarity = self.appearance_similarity(appearance, track.appearance)
                if similarity is not None and similarity < .2:
                    continue
                same_id = vessel.get("ship_id") == track.ship_id
                compatible_height = .45 <= ratio <= 2.3 or clipped and ratio < .45 and same_id and overlap >= .1
                if compatible_height and (overlap >= .1 or distance <= .08):
                    pairs.append((overlap - distance * 2 + (.05 if same_id else 0) + (.3 * similarity if similarity is not None else 0), index, key))
        assigned, used = {}, set()
        for _, index, key in sorted(pairs, reverse=True):
            if index not in assigned and key not in used:
                assigned[index], used = key, used | {key}
        ambiguous = set()
        for index, key in assigned.items():
            choices = sorted([score for score, candidate_index, _ in pairs if candidate_index == index], reverse=True)
            if len(choices) > 1 and choices[0] - choices[1] < .06:
                ambiguous.add(index)
        visible = []
        for index, (vessel, box, appearance) in enumerate(parsed):
            key = assigned.get(index)
            point = self._stable_point(box)
            if key is None:
                key, self.next_id = self.next_id, self.next_id + 1
                track = _Target(key, vessel.get("ship_id", 0), box, timestamp, point, point)
                self.tracks[key] = track
            else:
                track = self.tracks[key]
                gap = timestamp - track.last_seen
                continuous = reliable and 0 < gap <= self.MAX_OBSERVATION_GAP
                if not continuous:
                    track.history.clear()
                    track.anchor = track.motion_anchor = point
                    track.moving_seconds = 0.
                    if track.state != "stationary" and (track.last_moving_at is None or timestamp - track.last_moving_at > settings.motion_confirm_seconds * 2 + 1):
                        track.state = "observing"
                        track.still_seconds = 0.
                else:
                    threshold = settings.stationary_motion_threshold
                    # Evaluate displacement over a short evidence window, not
                    # per-frame velocity (which mistakes low-speed ships for berth).
                    track.history.append((timestamp, point))
                    while track.history and timestamp - track.history[0][0] > max(2., settings.motion_confirm_seconds * 3):
                        track.history.popleft()
                    positions = [value[1] for value in track.history]
                    steps = [math.dist(a, b) for a, b in zip(positions, positions[1:])]
                    path = sum(steps)
                    recent = math.dist(positions[0], positions[-1]) if positions else 0.
                    span = timestamp - track.history[0][0] if track.history else 0.
                    sustained = recent > threshold and span >= settings.motion_confirm_seconds and sum(step > threshold * .1 for step in steps) >= 2 and recent / max(path, 1e-9) >= .55
                    if sustained:
                        track.moving_seconds = span
                        track.still_seconds = 0.
                        track.anchor = point
                        track.state = "moving"
                        track.last_moving_at = timestamp
                    else:
                        track.moving_seconds = 0.
                        if math.dist(point, track.anchor) <= threshold:
                            track.still_seconds += gap
                        else:
                            track.anchor = point
                            track.still_seconds = 0.
                        if track.still_seconds >= settings.stationary_seconds:
                            track.state = "stationary"
                        elif track.state == "stationary":
                            pass
                        elif track.last_moving_at is not None and timestamp - track.last_moving_at <= settings.motion_confirm_seconds * 2 + 1:
                            track.state = "moving"
                        else:
                            track.state = "observing"
            track.box, track.last_seen, track.ship_id, track.vessel = box, timestamp, vessel.get("ship_id", 0), dict(vessel)
            if appearance is not None:
                if track.appearance is None:
                    track.appearance = appearance
                else:
                    mixed = .85 * np.asarray(track.appearance) + .15 * np.asarray(appearance)
                    track.appearance = tuple((mixed / max(1e-9, np.linalg.norm(mixed))).tolist())
            measured_height = box[3] - box[1]
            source_box = self.detection_box(vessel, width, height)
            height_clipped = bool(source_box and (source_box[1] <= .003 or source_box[3] >= .997))
            if height_clipped:
                # A clipped observation is a lower bound, at 1x as well as
                # after a lens move. Do not insert it into the full-height
                # history: repeated clipping must not gradually drive another
                # zoom-in. A larger lower bound still prompts a safe zoom-out.
                track.stable_height = max(track.stable_height, measured_height)
            else:
                track.heights.append(measured_height)
                # With only two observations median() averages both, allowing
                # one bad second box to halve/double the initial estimate.
                # Require a third sample before replacing that first estimate.
                if not track.stable_height or len(track.heights) >= 3:
                    track.stable_height = median(track.heights)
            track.reference_height = max(track.reference_height, track.stable_height)
            processing = vessel.get("processing") or {}
            processing_allowed = processing.get("eligible") is not False
            eligible = (not settings.suppress_stationary or track.state == "moving" and reliable) and index not in ambiguous and processing_allowed
            reason = str(processing.get("reason") or "target_filtered") if not processing_allowed else "ambiguous_identity" if index in ambiguous else "stationary_excluded" if track.state == "stationary" and settings.suppress_stationary else "uncertain_background" if not reliable else "observing_motion" if not eligible else "nearest_moving"
            visible.append({"target_id": key, "ship_id": track.ship_id, "box": list(box),
                            "height_ratio": track.stable_height, "height_clipped": height_clipped,
                            "motion_state": track.state,
                            "stationary_seconds": round(track.still_seconds, 3), "eligible": eligible, "reason": reason,
                            "identity_ambiguous": index in ambiguous,
                            "vessel": dict(vessel)})
        # Bound identity memory even in a busy port with detector ID churn.
        if len(self.tracks) > self.MAX_TRACKS:
            keep = sorted(self.tracks, key=lambda k: self.tracks[k].last_seen, reverse=True)[:self.MAX_TRACKS]
            self.tracks = {key: self.tracks[key] for key in keep}
        candidates = [target for target in visible if target["eligible"]]
        candidates.sort(key=lambda target: (target["height_ratio"], target["box"][3], target["vessel"].get("confidence", 0)), reverse=True)
        selected = candidates[0] if candidates else None
        old = next((target for target in candidates if target["target_id"] == self.selected_id), None)
        selection_reason = "nearest_moving" if selected else "no_moving_target"
        if old and selected and old["height_ratio"] * (1 + settings.target_switch_margin) >= selected["height_ratio"]:
            selected = old
            selection_reason = "keep_target_hysteresis"
        self.selected_id = selected["target_id"] if selected else None
        result = {"selected": {**selected["vessel"], **{k: v for k, v in selected.items() if k != "vessel"}} if selected else None,
                  "selected_target_id": self.selected_id, "targets": [{k: v for k, v in target.items() if k != "vessel"} for target in visible],
                  "zoom_factor": 1., "requested_zoom_factor": 1., "selection_reason": selection_reason}
        if selected:
            result.update(self.plan_zoom(selected, settings, calibrated_factors))
            result["target_selection_reason"] = selection_reason
        self.result = result
        return result
