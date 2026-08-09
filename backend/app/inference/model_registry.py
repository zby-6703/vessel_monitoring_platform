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

    def load(self) -> None:
        from .onnx_backends import ONNXDraftFormerPredictor, ONNXShipDETRPredictor, ONNXShipNameRecognizer

        ship_config, ship_weights = self._pair(self.settings.ship_detector_config, self.settings.ship_detector_weights, "ship detector")
        draft_config, draft_weights = self._pair(self.settings.draftformer_config, self.settings.draftformer_weights, "DraftFormer")
        name_config, name_weights = self._pair(self.settings.shipname_config, self.settings.shipname_weights, "ship-name recognizer")
        self.ship_detector = ONNXShipDETRPredictor(weights_path=ship_weights, config_path=ship_config, device=self.settings.device)
        self.draft_predictor = ONNXDraftFormerPredictor(weights_path=draft_weights, config_path=draft_config, device=self.settings.device)
        self.name_recognizer = ONNXShipNameRecognizer(weights_path=name_weights, config_path=name_config, device=self.settings.device)
        self.depth_estimator = StructureConstrainedVesselDraftDepthEstimation(class_names=self.draft_predictor.class_names, min_confidence=.3)

    def release(self) -> None:
        self.ship_detector = self.draft_predictor = self.name_recognizer = self.depth_estimator = None
        gc.collect()
