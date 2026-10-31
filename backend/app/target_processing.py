"""Route live detections to bounded, selective name and draft inference.

The detector and tracker still see every vessel. Qualification is measured in
the 1x coordinate plane so optical enlargement cannot promote a small target.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from statistics import median
import time

from .scene_motion import BackgroundMotionEstimator
from .vessel_selection import VesselTargetSelector


@dataclass
class _Evidence:
    last_seen: float
    serial: int
    since: float
    hits: int = 0
    qualified: bool = False
    lens_interrupted: bool = False
    last_detail: float | None = None
    heights: deque = field(default_factory=lambda: deque(maxlen=5))
    widths: deque = field(default_factory=lambda: deque(maxlen=5))
    last_box: tuple | None = None
    entry_side: str | None = None
    entry_complete: bool = False
    entry_max_width: float = 0.
    entry_growth_at: float = 0.


class RealtimeTargetProcessor:
    """One instance per capture/playback session; no global identity state."""

    # Camera timestamps are Unix seconds; subtraction at that magnitude can
    # lose several tenths of a microsecond even for exact sampling intervals.
    TIME_EPSILON = 1e-6

    POLICY_FIELDS = (
        "target_filter_enabled", "min_target_height_ratio", "min_target_width_ratio",
        "target_confirm_hits", "target_confirm_seconds", "target_roi_left",
        "target_roi_top", "target_roi_right", "target_roi_bottom",
        "detail_interval_seconds", "max_detail_targets", "confidence_threshold",
        "suppress_stationary", "stationary_seconds", "motion_confirm_seconds",
        "stationary_motion_threshold",
    )

    def __init__(self):
        self.selector = VesselTargetSelector()
        self.background = BackgroundMotionEstimator()
        self.evidence: dict[int, _Evidence] = {}
        self._policy = None
        self._generation = None
        self._last_time = None
        self._serial = 0

    @staticmethod
    def _live_vessel(vessel):
        x1, y1, x2, y2 = vessel.ship.xyxy
        return {"ship_id": vessel.track_id or vessel.vessel_id,
                "confidence": vessel.ship.confidence,
                "boxes": [{"type": "ship", "xywh": [x1, y1, x2 - x1, y2 - y1]}]}

    @staticmethod
    def _decision(reason, *, eligible=False, height=0., width=0., motion=None):
        return {"eligible": eligible, "reason": reason, "detail_inferred": False,
                "height_ratio": height, "width_ratio": width, "motion_state": motion}

    @staticmethod
    def _same_entry_geometry(previous, current):
        """An ID alone cannot transfer a bow-entry exemption to another hull."""
        old_w, old_h = previous[2] - previous[0], previous[3] - previous[1]
        new_w, new_h = current[2] - current[0], current[3] - current[1]
        overlap_x = min(previous[2], current[2]) - max(previous[0], current[0])
        overlap_y = min(previous[3], current[3]) - max(previous[1], current[1])
        return (old_w > 0 and old_h > 0 and .65 <= new_h / old_h <= 1.6
                and new_w >= old_w * .65
                and overlap_x >= min(old_w, new_w) * .2
                and overlap_y >= min(old_h, new_h) * .5)

    def _finish(self, result, started):
        decisions = [vessel.processing for vessel in result.vessels]
        eligible = sum(bool(value.get("eligible")) for value in decisions)
        detailed = sum(bool(value.get("detail_inferred")) for value in decisions)
        result.processing_summary = {
            "detected": len(decisions), "eligible": eligible, "detail_inferred": detailed,
            "lightweight": len(decisions) - eligible, "detail_skipped": eligible - detailed,
        }
        result.processing_ms += (time.perf_counter() - started) * 1000
        return result

    def process(self, service, result, image, timestamp, settings, observation_context=None):
        """Enrich selected vessels in an already detected/tracked frame in place.

        ``enrich_vessel(image, vessel, errors)`` returns a new vessel retaining
        its tracking ID. No cached OCR is copied into a later observation.
        """
        started = time.perf_counter()
        policy = tuple(getattr(settings, name) for name in self.POLICY_FIELDS)
        if policy != self._policy or self._last_time is not None and timestamp < self._last_time:
            self.selector = VesselTargetSelector()
            self.background.reset()
            self.evidence.clear()
            self._policy = policy
            self._generation = None
        self._serial += 1
        self._last_time = timestamp

        # Preserve legacy full recognition when the explicit filter is off.
        if not settings.target_filter_enabled:
            for index, vessel in enumerate(result.vessels):
                vessel.processing = self._decision("filter_disabled", eligible=True)
                enriched = service.enrich_vessel(image, vessel, result.errors)
                enriched.processing = {**vessel.processing, "detail_inferred": True}
                result.vessels[index] = enriched
            return self._finish(result, started)

        context = observation_context or {"zoom_factor": 1., "accept_detections": True, "generation": 0}
        zoom = context.get("zoom_factor")
        reason = None
        if context.get("accept_detections") is not True:
            reason = "lens_moving"
        elif not isinstance(zoom, (int, float)) or not math.isfinite(zoom) or zoom < 1:
            reason = "zoom_unknown"
        elif not math.isfinite(timestamp):
            reason = "stale_observation"
        if reason:
            self.background.reset()
            for evidence in self.evidence.values():
                evidence.hits = 0
                # Settling changes visibility but is not evidence that a
                # previously measured full hull became physically smaller.
                # An unknown/manual view cannot make the same guarantee.
                evidence.lens_interrupted = reason == "lens_moving"
                evidence.entry_side = None
                evidence.entry_complete = True
                if reason != "lens_moving":
                    evidence.qualified = False
            for vessel in result.vessels:
                vessel.processing = self._decision(reason)
            return self._finish(result, started)

        generation = context.get("generation", 0)
        if self._generation != generation:
            for evidence in self.evidence.values():
                evidence.hits = 0
                evidence.since = timestamp
            self.background.reset()
            self._generation = generation
        live = [self._live_vessel(vessel) for vessel in result.vessels]
        background = self.background.update(image, live, timestamp, generation=generation)
        selected = self.selector.update(live, result.image_width, result.image_height, timestamp,
            settings, zoom_factor=zoom, camera_motion=background, image=image)
        targets = list(selected["targets"])
        candidates = []
        seen_keys = set()
        for index, (vessel, live_vessel) in enumerate(zip(result.vessels, live)):
            box = self.selector.detection_box(live_vessel, result.image_width, result.image_height, zoom)
            source_box = self.selector.detection_box(live_vessel, result.image_width, result.image_height)
            matches = [target for target in targets if target["target_id"] not in seen_keys
                       and target["ship_id"] == live_vessel["ship_id"]]
            target = min(matches, key=lambda item: sum(abs(a - b) for a, b in zip(item["box"], box))) if matches and box else None
            if target is None:
                vessel.processing = self._decision("low_confidence" if box else "invalid_target")
                continue
            key = target["target_id"]
            seen_keys.add(key)
            evidence = self.evidence.get(key)
            continuous = evidence is not None and evidence.serial == self._serial - 1 and 0 < timestamp - evidence.last_seen <= self.selector.MAX_OBSERVATION_GAP
            if evidence is None:
                evidence = self.evidence[key] = _Evidence(timestamp, self._serial, timestamp)
            gap = timestamp - evidence.last_seen
            if gap > self.selector.MAX_OBSERVATION_GAP:
                # Treat a true identity gap as a new arrival, never as a
                # continuing partial hull based on a recycled upstream ID.
                evidence.last_box = None
                evidence.entry_side = None
                evidence.entry_complete = False
            elif evidence.last_box is not None and not self._same_entry_geometry(evidence.last_box, box):
                evidence.entry_side = None
                evidence.entry_complete = True
            if not continuous:
                preserve_geometry = (evidence.lens_interrupted and evidence.qualified
                    and timestamp - evidence.last_seen <= max(30., settings.zoom_settle_timeout_seconds + 2)
                    and not target.get("identity_ambiguous"))
                evidence.hits = 0
                evidence.since = timestamp
                if not preserve_geometry:
                    evidence.qualified = False
                    evidence.heights.clear()
                    evidence.widths.clear()
            evidence.lens_interrupted = False
            measured = box[3] - box[1]
            # A vertically clipped zoomed view provides a lower bound only.
            clipped = source_box[1] <= .003 or source_box[3] >= .997
            if not clipped or not evidence.heights:
                evidence.heights.append(measured)
            horizontal_clipped = source_box[0] <= .003 or source_box[2] >= .997
            # Reacquiring a known full-width hull in a tighter optical view
            # must not reinterpret the crop width as the actual hull size.
            if zoom == 1 or not horizontal_clipped or not evidence.widths:
                evidence.widths.append(box[2] - box[0])
            stable_height = median(evidence.heights)
            stable_width = median(evidence.widths)
            if zoom != 1:
                evidence.entry_side = None
                evidence.entry_complete = True
            elif (evidence.last_box is None and not evidence.entry_complete and horizontal_clipped):
                evidence.entry_side = "left" if source_box[0] <= .003 else "right"
                evidence.entry_max_width = box[2] - box[0]
                evidence.entry_growth_at = timestamp
            if evidence.entry_side is not None:
                measured_width = box[2] - box[0]
                if measured_width >= evidence.entry_max_width + .001:
                    evidence.entry_max_width = measured_width
                    evidence.entry_growth_at = timestamp
                # A bow may move a few pixels off the boundary before motion
                # and detection confirmation finish. Keep its established
                # exemption while the visible hull continues to grow, rather
                # than waiting for the full-width threshold again. A narrow
                # fully visible boat must not keep that exemption indefinitely.
                idle_limit = max(1.5, settings.target_confirm_seconds + settings.motion_confirm_seconds + .5)
                if (stable_width >= settings.min_target_width_ratio
                        or timestamp - evidence.entry_growth_at > idle_limit):
                    evidence.entry_side = None
                    evidence.entry_complete = True
            evidence.last_box = box
            evidence.last_seen, evidence.serial = timestamp, self._serial
            factor = .8 if evidence.qualified else 1.
            height_ok = stable_height >= settings.min_target_height_ratio * factor
            horizontal_entry = zoom == 1 and evidence.entry_side is not None
            width_ok = horizontal_entry or stable_width >= settings.min_target_width_ratio * factor
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            inside = settings.target_roi_left <= cx <= settings.target_roi_right and settings.target_roi_top <= cy <= settings.target_roi_bottom
            reason = "eligible"
            if not height_ok or not width_ok:
                reason = "small_target"
            elif not inside:
                reason = "outside_monitoring_area"
            elif target.get("identity_ambiguous"):
                reason = "ambiguous_identity"
            elif settings.suppress_stationary and target["motion_state"] == "stationary":
                reason = "stationary_excluded"
            if reason != "eligible":
                evidence.hits, evidence.qualified = 0, False
                evidence.since = timestamp
            else:
                evidence.hits += 1
                if evidence.hits < settings.target_confirm_hits or timestamp - evidence.since + self.TIME_EPSILON < settings.target_confirm_seconds:
                    reason = "waiting_stability"
                else:
                    evidence.qualified = True
            vessel.processing = self._decision(reason, eligible=reason == "eligible", height=stable_height,
                width=stable_width, motion=target["motion_state"])
            vessel.processing["target_id"] = key
            vessel.processing["hits"] = evidence.hits
            vessel.processing["horizontal_entry_exemption"] = horizontal_entry
            if reason == "eligible":
                if evidence.last_detail is not None and timestamp - evidence.last_detail + self.TIME_EPSILON < settings.detail_interval_seconds:
                    vessel.processing["reason"] = "detail_cooldown"
                else:
                    candidates.append((index, evidence, stable_height, key))

        # Prefer larger hulls for equally overdue work, then rotate to the
        # least recently serviced target to avoid starvation in a busy scene.
        candidates.sort(key=lambda value: (value[1].last_detail if value[1].last_detail is not None
                                           else value[1].since - settings.detail_interval_seconds - .001,
                                           -value[2], value[3]))
        for ordinal, (index, evidence, _, _) in enumerate(candidates):
            vessel = result.vessels[index]
            if ordinal >= settings.max_detail_targets:
                vessel.processing["reason"] = "detail_budget"
                continue
            enriched = service.enrich_vessel(image, vessel, result.errors)
            enriched.processing = {**vessel.processing, "detail_inferred": True}
            result.vessels[index] = enriched
            evidence.last_detail = timestamp

        self.evidence = {key: value for key, value in self.evidence.items()
                         if key in self.selector.tracks and timestamp - value.last_seen <= 30}
        if len(self.evidence) > self.selector.MAX_TRACKS:
            keys = sorted(self.evidence, key=lambda key: self.evidence[key].last_seen, reverse=True)[:self.selector.MAX_TRACKS]
            self.evidence = {key: self.evidence[key] for key in keys}
        return self._finish(result, started)
