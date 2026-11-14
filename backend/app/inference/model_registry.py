"""Long-lived ONNX model holder for the single-frame inference service."""
from __future__ import annotations

import gc
from pathlib import Path

from ..config import Settings
from ..tracking import StructureConstrainedVesselDraftDepthEstimation


class ModelRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ship_detector = self.draft_predictor = self.name_recognizer = self.depth_estimator = None

    @staticmethod
    def _pair(config: Path | None, weights: Path | None, name: str) -> tuple[str, str]:
        if not config or not config.is_file() or not weights or not weights.is_file():
            raise FileNotFoundError(f"{name} config/weights unavailable: {config}, {weights}")
        if weights.suffix.lower() != ".onnx":
            raise ValueError(f"{name} requires ONNX weights")
        return str(config), str(weights)

    def load(self, *, realtime: bool = False) -> None:
        from .onnx_backends import ONNXShipDETRPredictor

        ship_config, ship_weights = self._pair(self.settings.ship_detector_config, self.settings.ship_detector_weights, "ship detector")
        self.ship_detector = ONNXShipDETRPredictor(weights_path=ship_weights, config_path=ship_config, device=self.settings.device)
        if not realtime:
            self.ensure_details(enable_ship_name=True, enable_draft=True)

    def ensure_details(self, *, enable_ship_name: bool, enable_draft: bool) -> None:
        from .onnx_backends import ONNXDraftFormerPredictor, ONNXShipNameRecognizer

        if enable_draft and self.draft_predictor is None:
            draft_config, draft_weights = self._pair(self.settings.draftformer_config, self.settings.draftformer_weights, "DraftFormer")
            predictor = ONNXDraftFormerPredictor(weights_path=draft_weights, config_path=draft_config, device=self.settings.device)
            self.depth_estimator = StructureConstrainedVesselDraftDepthEstimation(class_names=predictor.class_names, min_confidence=.3)
            self.draft_predictor = predictor
        if enable_ship_name and self.name_recognizer is None:
            name_config, name_weights = self._pair(self.settings.shipname_config, self.settings.shipname_weights, "ship-name recognizer")
            self.name_recognizer = ONNXShipNameRecognizer(weights_path=name_weights, config_path=name_config, device=self.settings.device)

    def trim_details(self, *, enable_ship_name: bool, enable_draft: bool) -> None:
        """Called only by the owning inference worker, between observations."""
        if not enable_ship_name:
            self.name_recognizer = None
        if not enable_draft:
            self.draft_predictor = self.depth_estimator = None

    def release(self) -> None:
        self.ship_detector = self.draft_predictor = self.name_recognizer = self.depth_estimator = None
        gc.collect()
