"""Pure, reusable single-frame inference for the v2 pipeline."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import cv2
import numpy as np
from PIL import Image

from ..schemas import (
    Detection,
    DraftObservation,
    FrameResultV2,
    NameObservation,
    TaskKind,
    VesselFrameResult,
    WaterPolygon,
    sample_id_for,
    normalize_ship_name,
    recognized_name_fields,
)


def _array(value: Any) -> np.ndarray:
    """Accept ONNX/Torch values and small NumPy fakes used by unit tests."""
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


class FrameInferenceService:
    """Run models for exactly one OpenCV BGR frame without side effects."""

    DETECTOR_LABELS = {"ship": "ship", "name": "ship_name_area", "waterlineArea": "waterline_area"}

    def __init__(self, models: Any, min_confidence: float = 0.3) -> None:
        self.models = models
        self.min_confidence = min_confidence

    def infer(
        self,
        frame: np.ndarray,
        *,
        task_id: str,
        source_filename: str,
        task: TaskKind,
        observed_at: datetime,
        frame_index: int | None = None,
        source_frame_index: int | None = None,
    ) -> FrameResultV2:
        if frame is None or frame.ndim < 2:
            raise ValueError("frame must be a non-empty OpenCV ndarray")
        started = time.perf_counter()
        height, width = frame.shape[:2]
        errors: list[str] = []
        try:
            vessels = self._infer_vessels(frame, task, errors)
        except Exception as exc:  # a failed model must not discard this sample
            vessels = []
            errors.append(f"frame_inference_failed: {exc}")
        return FrameResultV2(
            task_id=task_id,
            source_filename=source_filename,
            sample_id=sample_id_for(source_filename, frame_index),
            frame_index=frame_index,
            source_frame_index=source_frame_index,
            observed_at=observed_at,
            image_width=width,
            image_height=height,
            processing_ms=(time.perf_counter() - started) * 1000,
            vessels=vessels,
            errors=errors,
        )

    def _infer_vessels(self, frame: np.ndarray, task: TaskKind, errors: list[str]) -> list[VesselFrameResult]:
        if task is TaskKind.SHIP_NAME_RECOGNITION:
            return [self._direct_name_vessel(frame, errors)]
        if task is TaskKind.DRAFT_ESTIMATION:
            return [self._direct_draft_vessel(frame, errors)]

        detections = self._detect(frame, errors)
        ships = [item for item in detections if item.label == "ship"]
        regions = [item for item in detections if item.label != "ship"]
        if task is TaskKind.REGION_DETECTION:
            return [
                VesselFrameResult(vessel_id=index, ship=ship, regions=self._regions_for_ship(ship, regions))
                for index, ship in enumerate(ships, start=1)
            ]
        vessels: list[VesselFrameResult] = []
        for index, ship in enumerate(ships, start=1):
            assigned = self._regions_for_ship(ship, regions)
            vessels.append(self._full_vessel(index, frame, ship, assigned, errors))
        return vessels

    def _detect(self, frame: np.ndarray, errors: list[str]) -> list[Detection]:
        try:
            output = self.models.ship_detector(frame)
            boxes = _array(output["boxes"]).reshape(-1, 4)
            scores = _array(output["scores"]).reshape(-1)
            labels = _array(output["labels"]).reshape(-1)
        except Exception as exc:
            errors.append(f"ship_detector_failed: {exc}")
            return []
        names = getattr(self.models.ship_detector, "class_names", [])
        results = []
        for box, score, label_id in zip(boxes, scores, labels):
            if float(score) < self.min_confidence:
                continue
            name = str(names[int(label_id)]) if 0 <= int(label_id) < len(names) else f"class_{int(label_id)}"
            kind = self.DETECTOR_LABELS.get(name)
            if not kind:
                continue
            x1, y1, x2, y2 = [float(value) for value in box]
            results.append(Detection(label=kind, xyxy=(x1, y1, max(x1, x2), max(y1, y2)), confidence=float(score), class_name=name))
        return results

    @staticmethod
    def _regions_for_ship(ship: Detection, regions: list[Detection]) -> list[Detection]:
        x1, y1, x2, y2 = ship.xyxy
        output = []
        for region in regions:
            rx1, ry1, rx2, ry2 = region.xyxy
            center_x, center_y = (rx1 + rx2) / 2, (ry1 + ry2) / 2
            if x1 <= center_x <= x2 and y1 <= center_y <= y2:
                output.append(region)
        return output

    def _full_vessel(self, vessel_id: int, frame: np.ndarray, ship: Detection, regions: list[Detection], errors: list[str]) -> VesselFrameResult:
        name_region = next((item for item in regions if item.label == "ship_name_area"), None)
        draft_region = next((item for item in regions if item.label == "waterline_area"), None)
        name, name_observation = self._recognize_name(frame, name_region, errors)
        draft_marks, water, draft = self._estimate_draft(frame, draft_region, errors)
        zh, en = recognized_name_fields(name)
        return VesselFrameResult(
            vessel_id=vessel_id,
            ship=ship,
            regions=regions,
            draft_marks=draft_marks,
            water=water,
            recognized_zh=zh,
            recognized_en=en,
            name_observation=name_observation,
            draft=draft,
        )

    def _direct_name_vessel(self, frame: np.ndarray, errors: list[str]) -> VesselFrameResult:
        height, width = frame.shape[:2]
        region = Detection(label="ship_name_area", xyxy=(0, 0, float(width), float(height)), confidence=1.0)
        value, observation = self._recognize_name(frame, region, errors)
        zh, en = recognized_name_fields(value)
        return VesselFrameResult(vessel_id=1, ship=Detection(label="ship", xyxy=(0, 0, float(width), float(height)), confidence=1.0), regions=[region], recognized_zh=zh, recognized_en=en, name_observation=observation)

    def _direct_draft_vessel(self, frame: np.ndarray, errors: list[str]) -> VesselFrameResult:
        height, width = frame.shape[:2]
        region = Detection(label="waterline_area", xyxy=(0, 0, float(width), float(height)), confidence=1.0)
        marks, water, draft = self._estimate_draft(frame, region, errors)
        return VesselFrameResult(vessel_id=1, ship=Detection(label="ship", xyxy=(0, 0, float(width), float(height)), confidence=1.0), regions=[region], draft_marks=marks, water=water, draft=draft)

    def _recognize_name(self, frame: np.ndarray, region: Detection | None, errors: list[str]) -> tuple[str, NameObservation | None]:
        if region is None:
            return "", None
        try:
            crop = self._crop(frame, region.xyxy)
            values = self.models.name_recognizer(img_numpy=Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)), batch_num=1)
            value = values[0] if values else {}
            text = str(value.get("text", ""))
            score = float(value.get("score", 0.0))
            return text, NameObservation(text=text, normalized=normalize_ship_name(text), roi_confidence=region.confidence, ocr_confidence=score)
        except Exception as exc:
            errors.append(f"ship_name_ocr_failed: {exc}")
            return "", NameObservation(roi_confidence=region.confidence)

    def _estimate_draft(self, frame: np.ndarray, region: Detection | None, errors: list[str]) -> tuple[list[Detection], list[WaterPolygon], DraftObservation]:
        if region is None:
            return [], [], DraftObservation()
        try:
            crop = self._crop(frame, region.xyxy)
            prediction = self.models.draft_predictor.predict(crop)
            items = list(prediction.detection.boxes)
            offset = np.asarray([region.xyxy[0], region.xyxy[1], region.xyxy[0], region.xyxy[1]])
            marks = [
                Detection(label="draft_mark", xyxy=tuple((np.asarray(item.xyxy, dtype=float) + offset).tolist()), confidence=float(item.score), class_name=str(item.class_name))
                for item in items if float(item.score) >= self.min_confidence
            ]
            boxes = np.asarray([item.xyxy for item in items], dtype=np.float32).reshape(-1, 4)
            labels = np.asarray([item.label for item in items], dtype=np.int64)
            scores = np.asarray([item.score for item in items], dtype=np.float32)
            mask = prediction.segmentation.get_binary_mask(threshold=0.5).astype(np.uint8)
            estimate = self.models.depth_estimator.estimate_with_details(boxes, labels, scores, mask)
            mean_confidence = float(scores.mean()) if len(scores) else 0.0
            return marks, self._mask_polygons(mask, region.xyxy), DraftObservation(depth_m=float(estimate.depth) if estimate.depth is not None else None, success=bool(estimate.success), roi_confidence=region.confidence, character_confidence=mean_confidence, method=str(getattr(estimate, "method", "")) or None)
        except Exception as exc:
            errors.append(f"draft_estimation_failed: {exc}")
            return [], [], DraftObservation(roi_confidence=region.confidence)

    @staticmethod
    def _crop(frame: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray:
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = (max(0, int(round(value))) for value in box)
        x1, x2 = min(x1, width), min(x2, width)
        y1, y2 = min(y1, height), min(y2, height)
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            raise ValueError("empty detected region")
        return crop

    @staticmethod
    def _mask_polygons(mask: np.ndarray, offset: tuple[float, float, float, float], min_area: float = 16.0) -> list[WaterPolygon]:
        contours, _ = cv2.findContours((mask > 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        polygons = []
        for contour in contours:
            if cv2.contourArea(contour) < min_area:
                continue
            simplified = cv2.approxPolyDP(contour, 1.5, True).reshape(-1, 2)
            if len(simplified) < 3:
                continue
            polygons.append(WaterPolygon(points=[(float(x + offset[0]), float(y + offset[1])) for x, y in simplified]))
        return polygons
