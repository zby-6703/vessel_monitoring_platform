"""TrackTrack-style tracker adapted to ShipDETR detections.

The association, confidence-adaptive Kalman update, track-aware initialisation
and iterative mutual-nearest matching follow the official TrackTrack tracker
(CVPR 2025).  The official FastReID embeddings are unavailable in this
workspace, so online video inference uses a deterministic HSV descriptor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

NEW, TRACKED, LOST, REMOVED = "new", "tracked", "lost", "removed"


@dataclass
class Detection:
    """One detector output in absolute xyxy image coordinates."""

    tlbr: np.ndarray
    score: float
    class_id: int = -1
    class_name: str = ""

    def __post_init__(self) -> None:
        self.tlbr = np.asarray(self.tlbr, dtype=np.float32).reshape(4)
        self.score = float(self.score)
        self.class_id = int(self.class_id)

    @property
    def tlwh(self) -> np.ndarray:
        x1, y1, x2, y2 = self.tlbr
        return np.asarray([x1, y1, x2 - x1, y2 - y1], dtype=np.float32)

    @property
    def area(self) -> float:
        width = max(float(self.tlbr[2] - self.tlbr[0]), 0.0)
        height = max(float(self.tlbr[3] - self.tlbr[1]), 0.0)
        return width * height


def _normalize(array: np.ndarray) -> np.ndarray:
    return array / max(float(np.linalg.norm(array)), 1e-12)


def _box_iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if not len(a) or not len(b):
        return np.zeros((len(a), len(b)), dtype=np.float32)
    lt = np.maximum(a[:, None, :2], b[None, :, :2])
    rb = np.minimum(a[:, None, 2:], b[None, :, 2:])
    wh = np.maximum(rb - lt, 0.0)
    inter = wh[..., 0] * wh[..., 1]
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-6)


def _corners(box: np.ndarray) -> np.ndarray:
    x1, y1, x2, y2 = box
    return np.asarray([[x1, y1], [x1, y2], [x2, y1], [x2, y2]], dtype=np.float32)


class ConfidenceKalmanFilter:
    """Official TrackTrack confidence-adaptive filter over cx, cy, w, h."""

    def __init__(self) -> None:
        self.motion = np.eye(8, dtype=np.float32)
        self.motion[np.arange(4), np.arange(4, 8)] = 1.0
        self.update_matrix = np.eye(4, 8, dtype=np.float32)
        self.std_pos, self.std_vel = 1.0 / 20.0, 1.0 / 160.0

    def initiate(self, measurement: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        mean = np.r_[measurement, np.zeros_like(measurement)].astype(np.float32)
        covariance = np.eye(8, dtype=np.float32)
        covariance[:4, :4] *= 2.0
        covariance[4:, 4:] *= 10.0
        covariance[[0, 2, 4, 6], [0, 2, 4, 6]] *= mean[2]
        covariance[[1, 3, 5, 7], [1, 3, 5, 7]] *= mean[3]
        return mean, np.square(covariance)

    def predict(self, mean: np.ndarray, covariance: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        mean = self.motion @ mean
        process = np.eye(8, dtype=np.float32)
        process[:4, :4] *= self.std_pos
        process[4:, 4:] *= self.std_vel
        process[[0, 2, 4, 6], [0, 2, 4, 6]] *= max(float(mean[2]), 1.0)
        process[[1, 3, 5, 7], [1, 3, 5, 7]] *= max(float(mean[3]), 1.0)
        return mean, self.motion @ covariance @ self.motion.T + np.square(process)

    def update(self, mean: np.ndarray, covariance: np.ndarray, measurement: np.ndarray, confidence: float) -> Tuple[np.ndarray, np.ndarray]:
        observation = self.update_matrix @ mean
        innovation = np.eye(4, dtype=np.float32) * self.std_pos
        innovation[[0, 2], [0, 2]] *= max(float(observation[2]), 1.0)
        innovation[[1, 3], [1, 3]] *= max(float(observation[3]), 1.0)
        projected = self.update_matrix @ covariance @ self.update_matrix.T + np.square(innovation) * max(1e-3, 1.0 - confidence)
        gain = covariance @ self.update_matrix.T @ np.linalg.pinv(projected)
        mean = mean + gain @ (measurement - observation)
        return mean.astype(np.float32), (covariance - gain @ projected @ gain.T).astype(np.float32)


@dataclass
class _Detection:
    tlbr: np.ndarray
    score: float
    class_id: int
    class_name: str
    feature: Optional[np.ndarray]

    @property
    def cxcywh(self) -> np.ndarray:
        x1, y1, x2, y2 = self.tlbr
        return np.asarray([(x1 + x2) / 2.0, (y1 + y2) / 2.0, max(x2 - x1, 1.0), max(y2 - y1, 1.0)], dtype=np.float32)


@dataclass
class TrackTrackTrack:
    detection: _Detection
    track_id: int
    frame_id: int
    kalman_filter: ConfidenceKalmanFilter
    state: str = NEW
    start_frame: int = field(init=False)
    end_frame_id: int = field(init=False)
    mean: np.ndarray = field(init=False)
    covariance: np.ndarray = field(init=False)
    history: Dict[int, Tuple[np.ndarray, float]] = field(default_factory=dict)
    feature: Optional[np.ndarray] = field(init=False)
    velocity: np.ndarray = field(default_factory=lambda: np.zeros((4, 2), dtype=np.float32))

    def __post_init__(self) -> None:
        self.start_frame = self.end_frame_id = self.frame_id
        self.mean, self.covariance = self.kalman_filter.initiate(self.detection.cxcywh)
        self.history[self.frame_id] = (self.detection.tlbr.copy(), self.detection.score)
        self.feature = None if self.detection.feature is None else self.detection.feature.copy()

    @property
    def tlbr(self) -> np.ndarray:
        cx, cy, width, height = self.mean[:4]
        return np.asarray([cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2], dtype=np.float32)

    @property
    def tlwh(self) -> np.ndarray:
        x1, y1, x2, y2 = self.tlbr
        return np.asarray([x1, y1, x2 - x1, y2 - y1], dtype=np.float32)

    @property
    def score(self) -> float:
        return float(self.detection.score)

    def predict(self) -> None:
        if self.state != TRACKED:
            self.mean[6:8] = 0.0
        self.mean, self.covariance = self.kalman_filter.predict(self.mean, self.covariance)

    def update(self, frame_id: int, detection: _Detection, min_len: int) -> None:
        self.mean, self.covariance = self.kalman_filter.update(self.mean, self.covariance, detection.cxcywh, detection.score)
        current_corners = _corners(detection.tlbr)
        velocity = np.zeros((4, 2), dtype=np.float32)
        for delta in range(1, 4):
            previous_key = frame_id - delta
            previous_box = self.history.get(previous_key, self.history[max(self.history)])[0]
            displacement = current_corners - _corners(previous_box)
            velocity += displacement / np.maximum(np.linalg.norm(displacement, axis=1, keepdims=True), 1e-5) / delta
        self.velocity = velocity / 3.0
        self.detection = detection
        self.history[frame_id] = (detection.tlbr.copy(), detection.score)
        # Association and velocity estimation only inspect the preceding three
        # frames.  Retaining the full video history leaks memory on live feeds.
        self.history = {key: value for key, value in self.history.items() if key >= frame_id - 3}
        if detection.feature is not None:
            self.feature = detection.feature.copy() if self.feature is None else _normalize(0.95 * self.feature + 0.05 * detection.feature)
        self.end_frame_id = frame_id
        self.state = TRACKED if len(self.history) >= min_len else NEW

    def to_result(self, frame_id: Optional[int] = None) -> dict:
        x, y, w, h = self.tlwh.tolist()
        return {"frame_id": int(self.end_frame_id if frame_id is None else frame_id), "track_id": int(self.track_id),
                "bbox": [float(x), float(y), float(w), float(h)], "bbox_xyxy": [float(v) for v in self.tlbr.tolist()],
                "score": self.score, "class_id": int(self.detection.class_id), "class_name": self.detection.class_name}


def _extract_features(image: Optional[np.ndarray], detections: Sequence[Detection]) -> List[Optional[np.ndarray]]:
    if image is None:
        return [None] * len(detections)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    height, width = hsv.shape[:2]
    result: List[Optional[np.ndarray]] = []
    for det in detections:
        x1, y1, x2, y2 = np.round(det.tlbr).astype(int)
        x1, x2, y1, y2 = max(0, x1), min(width, x2), max(0, y1), min(height, y2)
        if x2 - x1 < 2 or y2 - y1 < 2:
            result.append(None)
            continue
        hist = cv2.calcHist([hsv[y1:y2, x1:x2]], [0, 1, 2], None, [8, 8, 8], [0, 180, 0, 256, 0, 256]).reshape(-1)
        result.append(_normalize(hist.astype(np.float32)))
    return result


class TrackTrackTracker:
    """ShipDETR-compatible implementation of the official online TrackTrack stage."""

    def __init__(self, det_thresh: float = 0.5, init_thresh: Optional[float] = None, match_thresh: float = 0.7,
                 max_time_lost: int = 60, min_len: int = 3, min_box_area: float = 0.0, penalty_p: float = 0.20,
                 reduce_step: float = 0.05, tai_thresh: float = 0.55, appearance_weight: float = 0.5,
                 class_agnostic: bool = False) -> None:
        _validate_unit_interval("det_thresh", det_thresh)
        _validate_unit_interval("init_thresh", det_thresh if init_thresh is None else init_thresh)
        _validate_unit_interval("match_thresh", match_thresh)
        _validate_unit_interval("penalty_p", penalty_p)
        _validate_unit_interval("tai_thresh", tai_thresh)
        _validate_unit_interval("appearance_weight", appearance_weight)
        if max_time_lost < 0 or min_len < 1 or min_box_area < 0 or reduce_step <= 0:
            raise ValueError("max_time_lost/min_box_area must be non-negative, min_len and reduce_step must be positive")
        self.det_thresh, self.init_thresh, self.match_thresh = float(det_thresh), float(det_thresh if init_thresh is None else init_thresh), float(match_thresh)
        self.max_time_lost, self.min_len, self.min_box_area = int(max_time_lost), int(min_len), float(min_box_area)
        self.penalty_p, self.reduce_step, self.tai_thresh, self.appearance_weight = float(penalty_p), float(reduce_step), float(tai_thresh), float(appearance_weight)
        self.class_agnostic = bool(class_agnostic)
        self.frame_id, self.next_track_id, self.kalman_filter = 0, 1, ConfidenceKalmanFilter()
        self.tracks: List[TrackTrackTrack] = []

    def reset(self) -> None:
        """Clear all state before processing an unrelated video sequence."""
        self.frame_id, self.next_track_id = 0, 1
        self.tracks = []

    def update(self, detections: Sequence[Detection], frame_id: Optional[int] = None, image: Optional[np.ndarray] = None) -> List[TrackTrackTrack]:
        next_frame_id = self.frame_id + 1 if frame_id is None else int(frame_id)
        if next_frame_id <= self.frame_id:
            raise ValueError(f"frame_id must be strictly increasing: got {next_frame_id} after {self.frame_id}")
        frame_delta = next_frame_id - self.frame_id
        self.frame_id = next_frame_id
        clean = [det for det in detections if det.score > 0 and det.area >= self.min_box_area and np.all(np.isfinite(det.tlbr)) and det.tlbr[2] > det.tlbr[0] and det.tlbr[3] > det.tlbr[1]]
        dets = [_Detection(det.tlbr.copy(), det.score, det.class_id, det.class_name, feature) for det, feature in zip(clean, _extract_features(image, clean))]
        high, low = [d for d in dets if d.score > self.det_thresh], [d for d in dets if d.score <= self.det_thresh]
        track_pool = [track for track in self.tracks if track.state in (TRACKED, LOST)]
        new_tracks = [track for track in self.tracks if track.state == NEW]
        for track in track_pool + new_tracks:
            for _ in range(frame_delta):
                track.predict()
        matches, unmatched_tracks, unmatched_dets = self._associate(track_pool, high, low)
        all_dets = high + low
        for track_index, det_index in matches:
            track_pool[track_index].update(self.frame_id, all_dets[det_index], self.min_len)
        for track_index in unmatched_tracks:
            track_pool[track_index].state = LOST
        high_left = [high[index] for index in unmatched_dets if index < len(high)]
        matches, _, unmatched_high = self._associate(new_tracks, high_left, [])
        for track_index, det_index in matches:
            new_tracks[track_index].update(self.frame_id, high_left[det_index], self.min_len)
        # A tentative track needs several observations to activate.  Keeping it
        # alive through max_time_lost makes that initialisation robust to a
        # single missed detection.
        self.tracks = [track for track in self.tracks if track.state != REMOVED and self.frame_id - track.end_frame_id <= self.max_time_lost]
        self._init_tracks([high_left[index] for index in unmatched_high])
        return [track for track in self.tracks if track.state == TRACKED]

    def _associate(self, tracks: Sequence[TrackTrackTrack], high: Sequence[_Detection], low: Sequence[_Detection]) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
        detections = list(high) + list(low)
        if not tracks or not detections:
            return [], list(range(len(tracks))), list(range(len(detections)))
        iou = _box_iou(np.asarray([track.tlbr for track in tracks]), np.asarray([det.tlbr for det in detections]))
        cost = 1.0 - iou
        if self.appearance_weight and all(track.feature is not None for track in tracks) and all(det.feature is not None for det in detections):
            track_features = np.stack([track.feature for track in tracks])
            det_features = np.stack([det.feature for det in detections])
            appearance = np.clip(1.0 - track_features @ det_features.T, 0.0, 1.0)
            cost = (1.0 - self.appearance_weight) * cost + self.appearance_weight * appearance
        previous_scores = np.asarray([track.history[sorted(track.history)[-min(2, len(track.history))]][1] for track in tracks])
        cost += 0.10 * np.abs(np.asarray([track.score for track in tracks])[:, None] * 2.0 - previous_scores[:, None] - np.asarray([det.score for det in detections])[None, :])
        # TrackTrack's motion-direction term compares the four box-corner
        # velocities over a three-frame interval.
        angle_cost = np.zeros_like(cost)
        for track_index, track in enumerate(tracks):
            previous_box = track.history.get(self.frame_id - 3, track.history[max(track.history)])[0]
            for det_index, det in enumerate(detections):
                displacement = _corners(det.tlbr) - _corners(previous_box)
                det_velocity = displacement / np.maximum(np.linalg.norm(displacement, axis=1, keepdims=True), 1e-5)
                cosine = np.sum(track.velocity * det_velocity, axis=1)
                angle_cost[track_index, det_index] = np.mean(np.abs(np.arccos(np.clip(cosine, -1.0, 1.0))) / np.pi) * det.score
        cost += 0.05 * angle_cost
        cost[:, len(high):] += self.penalty_p
        class_ok = np.ones_like(iou, dtype=bool) if self.class_agnostic else (
            np.asarray([track.detection.class_id for track in tracks])[:, None]
            == np.asarray([det.class_id for det in detections])[None, :]
        )
        cost[(iou <= 0.10) | ~class_ok] = 1.0
        cost = np.clip(cost, 0.0, 1.0)
        matches: List[Tuple[int, int]] = []
        threshold = self.match_thresh
        while threshold > 0.0:
            row_best, col_best = np.argmin(cost, axis=1), np.argmin(cost, axis=0)
            pairs = [(row, int(col)) for row, col in enumerate(row_best) if col_best[col] == row and cost[row, col] < threshold]
            if pairs:
                matches.extend(pairs)
                for row, col in pairs:
                    cost[row, :] = 1.0
                    cost[:, col] = 1.0
            else:
                threshold -= self.reduce_step
        used_tracks, used_detections = {row for row, _ in matches}, {col for _, col in matches}
        return matches, [i for i in range(len(tracks)) if i not in used_tracks], [i for i in range(len(detections)) if i not in used_detections]

    def _init_tracks(self, detections: Sequence[_Detection]) -> None:
        alive = [track for track in self.tracks if track.state in (TRACKED, NEW)]
        accepted: List[_Detection] = []
        for detection in sorted((det for det in detections if det.score > self.init_thresh), key=lambda det: det.score, reverse=True):
            boxes = [track.tlbr for track in alive] + [det.tlbr for det in accepted]
            if boxes and _box_iou(np.asarray([detection.tlbr]), np.asarray(boxes)).max() > self.tai_thresh:
                continue
            accepted.append(detection)
        for detection in accepted:
            track = TrackTrackTrack(detection, self.next_track_id, self.frame_id, self.kalman_filter)
            # The platform configures ``min_len=1`` for still-image and
            # offline jobs.  Such a sequence has no subsequent frame to
            # confirm a tentative track, so emit the first valid detection.
            if self.min_len <= 1:
                track.state = TRACKED
            self.tracks.append(track)
            self.next_track_id += 1


def _validate_unit_interval(name: str, value: float) -> None:
    value = float(value)
    if not np.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a finite value in [0, 1], got {value}")


def detections_from_arrays(
    boxes: np.ndarray,
    scores: np.ndarray,
    labels: Optional[np.ndarray] = None,
    class_names: Optional[Sequence[str]] = None,
) -> List[Detection]:
    """Convert detector tensors to validated :class:`Detection` objects."""

    boxes = np.asarray(boxes, dtype=np.float32)
    if boxes.size == 0:
        boxes = boxes.reshape(0, 4)
    elif boxes.ndim != 2 or boxes.shape[1] != 4:
        raise ValueError(f"boxes must have shape (N, 4), got {boxes.shape}")
    scores = np.asarray(scores, dtype=np.float32).reshape(-1)
    if len(scores) != len(boxes):
        raise ValueError(f"boxes and scores must have equal length, got {len(boxes)} and {len(scores)}")
    if labels is None:
        labels = np.full((len(boxes),), -1, dtype=np.int64)
    labels = np.asarray(labels, dtype=np.int64).reshape(-1)
    if len(labels) != len(boxes):
        raise ValueError(f"boxes and labels must have equal length, got {len(boxes)} and {len(labels)}")
    return [
        Detection(
            tlbr=box,
            score=float(score),
            class_id=int(label),
            class_name=str(class_names[int(label)]) if class_names is not None and 0 <= int(label) < len(class_names) else "",
        )
        for box, score, label in zip(boxes, scores, labels)
    ]
