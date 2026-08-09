"""ONNX Runtime backends that mirror the platform's PyTorch predictor APIs.

These adapters let ``ModelRegistry`` swap ``.pth`` weights for exported
``model.onnx`` packages under ``models/`` without changing callers in
``worker.py`` / ``standalone.py``.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

logger = logging.getLogger("vessel.inference.onnx")
_DLL_DIRECTORY_HANDLES: list[Any] = []


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class NumpyTensor:
    """Minimal stand-in for torch.Tensor used by existing worker/standalone code."""

    def __init__(self, array: np.ndarray):
        self._array = np.asarray(array)

    def detach(self) -> "NumpyTensor":
        return self

    def cpu(self) -> "NumpyTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self._array

    @property
    def shape(self):
        return self._array.shape

    def __len__(self) -> int:
        return len(self._array)


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_deploy_config(weights: Path, config: Path | None) -> Path:
    """Prefer sibling deploy_config.yaml next to model.onnx; else use provided config."""
    sibling = weights.parent / "deploy_config.yaml"
    if sibling.is_file():
        return sibling
    if config is not None and config.is_file():
        return config
    raise FileNotFoundError(
        f"No deploy_config.yaml next to {weights} and no valid config path: {config}"
    )


def _providers(device: str) -> list[str]:
    device = (device or "cpu").lower()
    if device.startswith("cuda") or device == "gpu":
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _register_torch_cuda_libraries() -> None:
    """Expose PyTorch's bundled CUDA DLLs to ONNX Runtime on Windows."""
    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return
    try:
        import torch

        torch_lib = Path(torch.__file__).resolve().parent / "lib"
        if torch_lib.is_dir():
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(torch_lib)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unable to register PyTorch CUDA library directory: %s", exc)


def _create_session(onnx_path: Path, device: str):
    providers = _providers(device)
    cuda_requested = providers[0] == "CUDAExecutionProvider"
    if cuda_requested:
        _register_torch_cuda_libraries()
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise ImportError(
            "onnxruntime is required for ONNX backends. "
            "Install with: pip install onnxruntime  (or onnxruntime-gpu)"
        ) from exc
    # Keep provider diagnostics in the API log, without enabling verbose ORT tracing.
    try:
        ort.set_default_logger_severity_level(3)  # 3 = ERROR
    except Exception:  # noqa: BLE001
        pass
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    try:
        session = ort.InferenceSession(str(onnx_path), sess_options=so, providers=providers)
    except Exception as exc:  # noqa: BLE001
        if cuda_requested:
            raise RuntimeError(
                f"CUDA was requested for {onnx_path.name}, but ONNX Runtime could not create "
                "CUDAExecutionProvider. Verify that the installed onnxruntime-gpu build matches "
                "the CUDA/cuDNN libraries in the Train environment. CPU fallback is disabled."
            ) from exc
        raise
    active = session.get_providers()
    if cuda_requested and "CUDAExecutionProvider" not in active:
        raise RuntimeError(
            f"CUDA was requested for {onnx_path.name}, but active ONNX providers are {active}. "
            "CPU fallback is disabled."
        )
    logger.info("Loaded ONNX %s with providers=%s", onnx_path.name, active)
    return session


def letterbox(image: np.ndarray, size_wh: tuple[int, int], pad_value: int = 0):
    target_w, target_h = int(size_wh[0]), int(size_wh[1])
    h, w = image.shape[:2]
    scale = min(target_w / max(w, 1), target_h / max(h, 1))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_left = (target_w - new_w) // 2
    pad_top = (target_h - new_h) // 2
    pad_right = target_w - new_w - pad_left
    pad_bottom = target_h - new_h - pad_top
    padded = cv2.copyMakeBorder(
        resized,
        pad_top,
        pad_bottom,
        pad_left,
        pad_right,
        cv2.BORDER_CONSTANT,
        value=(pad_value, pad_value, pad_value),
    )
    meta = {
        "scale": float(scale),
        "pad_left": int(pad_left),
        "pad_top": int(pad_top),
        "pad_right": int(pad_right),
        "pad_bottom": int(pad_bottom),
        "orig_h": int(h),
        "orig_w": int(w),
        "model_h": int(target_h),
        "model_w": int(target_w),
    }
    return padded, meta


def cxcywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    if boxes.size == 0:
        return np.zeros((0, 4), dtype=np.float32)
    cx, cy, w, h = boxes.T
    x1 = cx - w / 2.0
    y1 = cy - h / 2.0
    x2 = cx + w / 2.0
    y2 = cy + h / 2.0
    return np.stack([x1, y1, x2, y2], axis=1).astype(np.float32)


def restore_boxes(boxes_xyxy: np.ndarray, meta: dict) -> np.ndarray:
    if boxes_xyxy.size == 0:
        return boxes_xyxy.astype(np.float32)
    boxes = boxes_xyxy.astype(np.float32).copy()
    scale = max(meta["scale"], 1e-8)
    boxes[:, 0::2] = (boxes[:, 0::2] - meta["pad_left"]) / scale
    boxes[:, 1::2] = (boxes[:, 1::2] - meta["pad_top"]) / scale
    boxes[:, 0::2] = np.clip(boxes[:, 0::2], 0, meta["orig_w"])
    boxes[:, 1::2] = np.clip(boxes[:, 1::2], 0, meta["orig_h"])
    return boxes


def sigmoid(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, -50, 50)
    return 1.0 / (1.0 + np.exp(-x))


def nms_xyxy(boxes: np.ndarray, scores: np.ndarray, labels: np.ndarray, iou_thresh: float, class_aware: bool):
    if boxes.size == 0:
        return np.zeros((0,), dtype=np.int64)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        area_i = max(0.0, (boxes[i, 2] - boxes[i, 0])) * max(0.0, (boxes[i, 3] - boxes[i, 1]))
        area_r = np.maximum(0.0, boxes[rest, 2] - boxes[rest, 0]) * np.maximum(
            0.0, boxes[rest, 3] - boxes[rest, 1]
        )
        union = area_i + area_r - inter + 1e-8
        iou = inter / union
        if class_aware:
            same = labels[rest] == labels[i]
            suppress = (iou > iou_thresh) & same
        else:
            suppress = iou > iou_thresh
        order = rest[~suppress]
    return np.asarray(keep, dtype=np.int64)


# ---------------------------------------------------------------------------
# ShipDETR
# ---------------------------------------------------------------------------


def _shipdetr_class_names(config: dict) -> list[str]:
    """Read class metadata from both current and legacy deployment packages."""
    names = config.get("class_names") or []
    if names:
        return [str(name) for name in names]
    by_id = config.get("class_id_to_name") or {}
    if not by_id:
        return []
    return [str(name) for _, name in sorted(by_id.items(), key=lambda item: int(item[0]))]


class ONNXShipDETRPredictor:
    """Drop-in for ShipDETRPredictor: ``predictor(bgr_image) -> {boxes,scores,labels}``."""

    def __init__(
        self,
        weights_path: str | Path,
        config_path: str | Path | None = None,
        device: str = "cpu",
        score_thresh: float | None = None,
    ):
        self.weights_path = Path(weights_path)
        self.deploy_path = _resolve_deploy_config(self.weights_path, Path(config_path) if config_path else None)
        self.cfg = _load_yaml(self.deploy_path)
        self.device = device
        self.session = _create_session(self.weights_path, device)
        self.input_name = self.session.get_inputs()[0].name
        inp = self.cfg.get("input") or {}
        self.image_size_wh = tuple(inp.get("image_size_wh", [960, 540]))
        self.image_format = str(inp.get("image_format", "BGR")).upper()
        self.include_norm = bool(inp.get("include_norm_in_model", True))
        self.pixel_mean = np.asarray(inp.get("pixel_mean", [0, 0, 0]), dtype=np.float32).reshape(3, 1, 1)
        self.pixel_std = np.asarray(inp.get("pixel_std", [1, 1, 1]), dtype=np.float32).reshape(3, 1, 1)
        self.pad_value = int(inp.get("pad_value", 0))
        post = self.cfg.get("postprocess") or {}
        self.score_thresh = float(score_thresh if score_thresh is not None else post.get("score_thresh", 0.1))
        self.topk = int(post.get("topk", 100))
        self.class_names = _shipdetr_class_names(self.cfg)

    def preprocess(self, image: np.ndarray) -> tuple[np.ndarray, dict]:
        model_image = image
        if self.image_format == "RGB":
            model_image = cv2.cvtColor(model_image, cv2.COLOR_BGR2RGB)
        padded, meta = letterbox(model_image, self.image_size_wh, self.pad_value)
        tensor = padded.transpose(2, 0, 1).astype(np.float32)
        if not self.include_norm:
            tensor = (tensor - self.pixel_mean) / self.pixel_std
        return tensor[None, ...], meta

    def __call__(self, image: np.ndarray) -> dict[str, NumpyTensor]:
        tensor, meta = self.preprocess(image)
        logits, boxes = self.session.run(None, {self.input_name: tensor})
        logits = logits[0]
        boxes = boxes[0]
        prob = sigmoid(logits)
        scores = prob.max(axis=-1)
        labels = prob.argmax(axis=-1)
        keep = scores > self.score_thresh
        scores, labels, boxes = scores[keep], labels[keep], boxes[keep]
        if scores.size > self.topk:
            idx = np.argsort(-scores)[: self.topk]
            scores, labels, boxes = scores[idx], labels[idx], boxes[idx]
        boxes_xyxy = cxcywh_to_xyxy(boxes)
        boxes_xyxy[:, 0::2] *= meta["model_w"]
        boxes_xyxy[:, 1::2] *= meta["model_h"]
        boxes_xyxy = restore_boxes(boxes_xyxy, meta)
        return {
            "boxes": NumpyTensor(boxes_xyxy.astype(np.float32)),
            "scores": NumpyTensor(scores.astype(np.float32)),
            "labels": NumpyTensor(labels.astype(np.int64)),
        }


# ---------------------------------------------------------------------------
# DraftFormer
# ---------------------------------------------------------------------------


@dataclass
class _BoxPred:
    xyxy: np.ndarray
    score: float
    label: int
    class_name: str


@dataclass
class _DetPred:
    boxes: list


@dataclass
class _SegPred:
    sem_seg: np.ndarray
    orig_shape: tuple[int, int]

    def get_binary_mask(self, threshold: float = 0.5) -> np.ndarray:
        sem = self.sem_seg
        if sem.ndim == 3:
            sem = sem[0]
        if threshold == 0.5:
            max_value = float(sem.max()) if sem.size else 0.0
            if max_value > 1.0:
                threshold = max(0.1, max_value * 0.3)
        return (sem > threshold).astype(np.uint8)


@dataclass
class _MultiTaskPred:
    detection: _DetPred
    segmentation: _SegPred
    raw_output: dict
    image_path: str | None = None


class ONNXDraftFormerPredictor:
    """Drop-in for DraftFormerPredictor with ``predict()`` / ``class_names``."""

    def __init__(
        self,
        weights_path: str | Path,
        config_path: str | Path | None = None,
        device: str = "cpu",
        score_thresh: float | None = None,
    ):
        self.weights_path = Path(weights_path)
        self.deploy_path = _resolve_deploy_config(self.weights_path, Path(config_path) if config_path else None)
        self.cfg = _load_yaml(self.deploy_path)
        self.device = device
        self.session = _create_session(self.weights_path, device)
        self.input_name = self.session.get_inputs()[0].name
        inp = self.cfg.get("input") or {}
        self.image_size_wh = tuple(inp.get("image_size_wh", [256, 640]))
        self.image_format = str(inp.get("image_format", "BGR")).upper()
        self.include_norm = bool(inp.get("include_norm_in_model", True))
        self.pixel_mean = np.asarray(inp.get("pixel_mean", [0, 0, 0]), dtype=np.float32).reshape(3, 1, 1)
        self.pixel_std = np.asarray(inp.get("pixel_std", [1, 1, 1]), dtype=np.float32).reshape(3, 1, 1)
        self.pad_value = int(inp.get("pad_value", 0))
        post = self.cfg.get("postprocess") or {}
        self.score_thresh = float(score_thresh if score_thresh is not None else post.get("score_thresh", 0.1))
        self.topk = int(post.get("topk", 80))
        self.nms_thresh = float(post.get("nms_thresh", 0.5) or 0.0)
        self.nms_type = str(post.get("nms_type", "class_aware")).lower()
        tasks = self.cfg.get("tasks") or {}
        det_class_map = self.cfg.get("det_class_map") or {}
        if det_class_map:
            ordered = sorted(((int(class_id), str(name)) for name, class_id in det_class_map.items()), key=lambda item: item[0])
            self.class_names = [name for _, name in ordered]
        else:
            self.class_names = list(
                self.cfg.get("class_names")
                or (self.cfg.get("postprocess") or {}).get("detection_class_names")
                or []
            )
        self.det_class_map = {name: index for index, name in enumerate(self.class_names)}
        self.seg_class_map = {
            str(name): int(class_id)
            for name, class_id in (self.cfg.get("seg_class_map") or {"water": 0}).items()
        }
        self.det_q = int(tasks.get("detection_queries", 100))
        self.num_det = int(tasks.get("num_det_classes", len(self.class_names) or 9))
        self.num_seg = int(tasks.get("num_seg_classes", 1))

    def preprocess(self, image: np.ndarray) -> tuple[np.ndarray, dict]:
        model_image = image
        if self.image_format == "RGB":
            model_image = cv2.cvtColor(model_image, cv2.COLOR_BGR2RGB)
        padded, meta = letterbox(model_image, self.image_size_wh, self.pad_value)
        tensor = padded.transpose(2, 0, 1).astype(np.float32)
        if not self.include_norm:
            tensor = (tensor - self.pixel_mean) / self.pixel_std
        return tensor[None, ...], meta

    def __call__(self, image: np.ndarray) -> dict[str, NumpyTensor]:
        prediction = self.predict(image)
        boxes = np.asarray([b.xyxy for b in prediction.detection.boxes], dtype=np.float32).reshape(-1, 4)
        scores = np.asarray([b.score for b in prediction.detection.boxes], dtype=np.float32)
        labels = np.asarray([b.label for b in prediction.detection.boxes], dtype=np.int64)
        return {
            "boxes": NumpyTensor(boxes),
            "scores": NumpyTensor(scores),
            "labels": NumpyTensor(labels),
            "sem_seg": NumpyTensor(prediction.segmentation.sem_seg.astype(np.float32)),
            "image_size": prediction.segmentation.orig_shape,
        }

    def predict(self, image_or_path: Any) -> _MultiTaskPred:
        image_path = None
        if isinstance(image_or_path, (str, Path)):
            image_path = str(image_or_path)
            image = cv2.imread(image_path)
            if image is None:
                raise ValueError(f"Failed to read image: {image_path}")
        else:
            image = image_or_path

        tensor, meta = self.preprocess(image)
        logits, boxes, masks = self.session.run(None, {self.input_name: tensor})
        logits, boxes, masks = logits[0], boxes[0], masks[0]

        det_logits = logits[: self.det_q, : self.num_det]
        det_boxes = boxes[: self.det_q]
        prob = sigmoid(det_logits)
        flat = prob.reshape(-1)
        topk = min(self.topk, int(flat.size))
        if topk == 0:
            det_list: list[_BoxPred] = []
        else:
            idx = np.argpartition(-flat, topk - 1)[:topk]
            idx = idx[np.argsort(-flat[idx])]
            scores = flat[idx]
            labels = (idx % self.num_det).astype(np.int64)
            queries = (idx // self.num_det).astype(np.int64)
            keep = scores > self.score_thresh
            scores, labels, queries = scores[keep], labels[keep], queries[keep]
            sel = det_boxes[queries] if queries.size else np.zeros((0, 4), dtype=np.float32)
            boxes_xyxy = cxcywh_to_xyxy(sel)
            boxes_xyxy[:, 0::2] *= meta["model_w"]
            boxes_xyxy[:, 1::2] *= meta["model_h"]
            boxes_xyxy = restore_boxes(boxes_xyxy, meta)
            if scores.size and self.nms_type != "none" and self.nms_thresh > 0:
                keep_idx = nms_xyxy(
                    boxes_xyxy,
                    scores,
                    labels,
                    self.nms_thresh,
                    class_aware=self.nms_type == "class_aware",
                )
                boxes_xyxy = boxes_xyxy[keep_idx]
                scores = scores[keep_idx]
                labels = labels[keep_idx]
            det_list = []
            for box, score, label in zip(boxes_xyxy, scores, labels):
                name = (
                    self.class_names[int(label)]
                    if 0 <= int(label) < len(self.class_names)
                    else f"class_{int(label)}"
                )
                det_list.append(
                    _BoxPred(
                        xyxy=box.astype(np.float32),
                        score=float(score),
                        label=int(label),
                        class_name=str(name),
                    )
                )

        # Segmentation: queries after detection slots
        seg_logits = logits[self.det_q :, : self.num_seg]
        seg_masks = masks[self.det_q :]
        orig_h, orig_w = meta["orig_h"], meta["orig_w"]
        if seg_masks.size == 0:
            sem_seg = np.zeros((self.num_seg, orig_h, orig_w), dtype=np.float32)
        else:
            seg_prob = sigmoid(seg_logits)
            mask_prob = sigmoid(seg_masks)
            # [Q,C] x [Q,H,W] -> [C,H,W]
            sem = np.einsum("qc,qhw->chw", seg_prob, mask_prob).astype(np.float32)
            # upsample to letterbox then crop/resize to original
            up = np.stack(
                [
                    cv2.resize(ch, (meta["model_w"], meta["model_h"]), interpolation=cv2.INTER_LINEAR)
                    for ch in sem
                ],
                axis=0,
            )
            content_h = max(1, int(round(orig_h * meta["scale"])))
            content_w = max(1, int(round(orig_w * meta["scale"])))
            y0 = meta["pad_top"]
            x0 = meta["pad_left"]
            crop = up[:, y0 : y0 + content_h, x0 : x0 + content_w]
            sem_seg = np.stack(
                [
                    cv2.resize(ch, (orig_w, orig_h), interpolation=cv2.INTER_LINEAR)
                    for ch in crop
                ],
                axis=0,
            ).astype(np.float32)

        return _MultiTaskPred(
            detection=_DetPred(boxes=det_list),
            segmentation=_SegPred(sem_seg=sem_seg, orig_shape=(orig_h, orig_w)),
            raw_output={
                "boxes": NumpyTensor(np.asarray([b.xyxy for b in det_list], dtype=np.float32).reshape(-1, 4)),
                "scores": NumpyTensor(np.asarray([b.score for b in det_list], dtype=np.float32)),
                "labels": NumpyTensor(np.asarray([b.label for b in det_list], dtype=np.int64)),
                "sem_seg": NumpyTensor(sem_seg),
            },
            image_path=image_path,
        )


# ---------------------------------------------------------------------------
# Ship name recognition
# ---------------------------------------------------------------------------


class ONNXShipNameRecognizer:
    """Drop-in for OpenRecognizer ``__call__(img_numpy=PIL.Image, ...)`` API."""

    def __init__(
        self,
        weights_path: str | Path,
        config_path: str | Path | None = None,
        device: str = "cpu",
        character_dict_path: str | Path | None = None,
    ):
        self.weights_path = Path(weights_path)
        self.deploy_path = _resolve_deploy_config(self.weights_path, Path(config_path) if config_path else None)
        self.cfg = _load_yaml(self.deploy_path)
        self.device = device
        self.session = _create_session(self.weights_path, device)
        self.input_name = self.session.get_inputs()[0].name
        inp = self.cfg.get("input") or {}
        # RecTVResize uses [H, W]
        hw = inp.get("image_size_hw")
        if hw:
            self.img_h, self.img_w = int(hw[0]), int(hw[1])
        else:
            wh = inp.get("image_size_wh", [320, 48])
            self.img_w, self.img_h = int(wh[0]), int(wh[1])
        mean = inp.get("pixel_mean", [0.5, 0.5, 0.5])
        std = inp.get("pixel_std", [0.5, 0.5, 0.5])
        self.mean = float(mean[0]) if isinstance(mean, (list, tuple)) else float(mean)
        self.std = float(std[0]) if isinstance(std, (list, tuple)) else float(std)

        dict_path = character_dict_path or self.cfg.get("character_dict")
        if dict_path and not Path(dict_path).is_absolute():
            candidate = self.weights_path.parent / dict_path
            if candidate.is_file():
                dict_path = candidate
            else:
                post = self.cfg.get("postprocess") or {}
                dict_path = post.get("character_dict_path") or dict_path
        self.charset = self._load_ar_charset(Path(dict_path) if dict_path else None)

    @staticmethod
    def _load_ar_charset(path: Path | None) -> list[str]:
        chars: list[str] = []
        if path is not None and path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n\r")
                    if line != "":
                        chars.append(line)
        # ARLabelDecode: EOS + dict + BOS + PAD
        return ["</s>"] + chars + ["<s>", "<pad>"]

    def _preprocess_pil_or_numpy(self, image: Any) -> np.ndarray:
        # Accept PIL.Image or RGB/BGR ndarray
        if hasattr(image, "convert"):
            image = image.convert("RGB")
            arr = np.asarray(image)
        else:
            arr = np.asarray(image)
            if arr.ndim == 2:
                arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
            elif arr.shape[2] == 3:
                # Heuristic: OpenRecognizer path converts BGR->RGB before call.
                # If values look like BGR already RGB from platform, keep as RGB.
                pass
        h, w = arr.shape[:2]
        ratio = w / float(max(h, 1))
        if int(np.ceil(self.img_h * ratio)) > self.img_w:
            resized_w = self.img_w
        else:
            resized_w = max(1, int(np.ceil(self.img_h * ratio)))
        resized = cv2.resize(arr, (resized_w, self.img_h), interpolation=cv2.INTER_CUBIC)
        tensor = resized.astype(np.float32).transpose(2, 0, 1) / 255.0
        tensor = (tensor - self.mean) / self.std
        if resized_w < self.img_w:
            pad = np.zeros((3, self.img_h, self.img_w), dtype=np.float32)
            pad[:, :, :resized_w] = tensor
            tensor = pad
        return tensor.astype(np.float32)

    def _decode(self, logits: np.ndarray) -> tuple[str, float]:
        # logits: [T, C] — match ARLabelDecode: argmax + max-prob after softmax.
        ids = logits.argmax(axis=-1)
        shifted = logits - logits.max(axis=-1, keepdims=True)
        exp = np.exp(shifted)
        probs = exp / np.clip(exp.sum(axis=-1, keepdims=True), 1e-12, None)
        conf = probs.max(axis=-1)
        eos = self.charset.index("</s>") if "</s>" in self.charset else None
        bos = self.charset.index("<s>") if "<s>" in self.charset else None
        pad = self.charset.index("<pad>") if "<pad>" in self.charset else None
        chars: list[str] = []
        confs: list[float] = []
        for i, idx in enumerate(ids.tolist()):
            if eos is not None and idx == eos:
                break
            if bos is not None and idx == bos:
                continue
            if pad is not None and idx == pad:
                continue
            if idx < 0 or idx >= len(self.charset):
                continue
            ch = self.charset[idx]
            chars.append(ch)
            confs.append(float(conf[i]))
        text = "".join(chars)
        score = float(np.mean(confs)) if confs else 0.0
        return text, score

    def __call__(
        self,
        img_path=None,
        img_numpy_list=None,
        img_numpy=None,
        batch_num: int = 1,
    ):
        if img_numpy is not None:
            images = [img_numpy]
        elif img_numpy_list is not None:
            images = list(img_numpy_list)
        elif img_path is not None:
            path = Path(img_path)
            if path.is_dir():
                images = []
                for p in sorted(path.iterdir()):
                    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
                        images.append(cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB))
            else:
                bgr = cv2.imread(str(path))
                images = [cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)]
        else:
            raise ValueError("No input image path or numpy array.")

        results = []
        for start in range(0, len(images), batch_num):
            batch = images[start : start + batch_num]
            tensors = [self._preprocess_pil_or_numpy(img) for img in batch]
            max_h = max(t.shape[1] for t in tensors)
            max_w = max(t.shape[2] for t in tensors)
            padded = np.zeros((len(tensors), 3, max_h, max_w), dtype=np.float32)
            for i, t in enumerate(tensors):
                padded[i, :, : t.shape[1], : t.shape[2]] = t
            outputs = self.session.run(None, {self.input_name: padded})
            logits = outputs[0]
            for i in range(logits.shape[0]):
                text, score = self._decode(logits[i])
                results.append({"text": text, "score": score})
        return results


def is_onnx_weights(path: Path | str | None) -> bool:
    return bool(path) and Path(path).suffix.lower() == ".onnx"
