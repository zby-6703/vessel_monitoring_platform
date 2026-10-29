from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from app.inference.frame_inference import FrameInferenceService
from app.inference.onnx_backends import ONNXDraftFormerPredictor
from app.schemas import TaskKind
from app.tracking import StructureConstrainedVesselDraftDepthEstimation


NEW_CLASSES = ['0', '1', '2', '3', '4', '5', '6', '8']
OLD_CLASSES = [*NEW_CLASSES, 'M']
TEMPLATE = Path(__file__).resolve().parents[2] / 'docs/model-templates/draft_multitask.deploy.yaml'


def test_shipped_deployment_template_uses_eight_classes_with_m_merged_into_zero():
    config = yaml.safe_load(TEMPLATE.read_text(encoding='utf-8'))
    assert config['tasks']['num_det_classes'] == 8
    assert config['class_names'] == NEW_CLASSES
    assert config['det_class_map'] == {name: index for index, name in enumerate(NEW_CLASSES)}


def _write_package(folder, class_names, *, logit_classes=None, mask_queries=None, config_update=None):
    onnx = pytest.importorskip('onnx')
    pytest.importorskip('onnxruntime')
    config = yaml.safe_load(TEMPLATE.read_text(encoding='utf-8'))
    config['input']['image_size_wh'] = [128, 288]
    config['tasks'].update(detection_queries=13, num_det_classes=len(class_names))
    config['class_names'] = class_names
    config['det_class_map'] = {name: index for index, name in enumerate(class_names)}
    if config_update:
        config_update(config)
    (folder / 'deploy_config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')

    classes = logit_classes if logit_classes is not None else len(class_names)
    logits = np.full((1, 14, classes), -20., dtype=np.float32)
    boxes = np.zeros((1, 14, 4), dtype=np.float32)
    zero_symbol = 'M' if 'M' in class_names else '0'
    rows = [('4', '4'), ('4', '2'), ('4', zero_symbol), ('3', '8'), ('3', '6'), ('3', '4')]
    for row, characters in enumerate(rows):
        for column, character in enumerate(characters):
            query = row * 2 + column
            logits[0, query, class_names.index(character)] = 10.
            boxes[0, query] = [(40 + column * 24) / 128, (40 + row * 32) / 288, 12 / 128, 20 / 288]
    logits[0, 13, 0] = 20.
    masks = np.zeros((1, mask_queries if mask_queries is not None else 14, 72, 32), dtype=np.float32)
    masks[:, -1, :56, :] = -10.
    masks[:, -1, 56:, :] = 10.

    arrays = [logits, boxes, masks]
    names = ['pred_logits', 'pred_boxes', 'pred_masks']
    nodes = [
        onnx.helper.make_node('Constant', [], [name], value=onnx.numpy_helper.from_array(value))
        for name, value in zip(names, arrays)
    ]
    graph = onnx.helper.make_graph(
        nodes, 'synthetic_draft',
        [onnx.helper.make_tensor_value_info('images', onnx.TensorProto.FLOAT, [1, 3, 288, 128])],
        [onnx.helper.make_tensor_value_info(name, onnx.TensorProto.FLOAT, array.shape) for name, array in zip(names, arrays)],
    )
    model = onnx.helper.make_model(graph, opset_imports=[onnx.helper.make_opsetid('', 17)], ir_version=8)
    onnx.checker.check_model(model)
    path = folder / 'model.onnx'
    onnx.save(model, path)
    return path


@pytest.mark.parametrize('class_names', [NEW_CLASSES, OLD_CLASSES], ids=['merged-zero-8', 'legacy-M-9'])
def test_real_onnx_package_runs_through_frame_service_and_estimates_depth(tmp_path, class_names):
    predictor = ONNXDraftFormerPredictor(_write_package(tmp_path, class_names), device='cpu')
    assert predictor.class_names == class_names
    frame = np.full((288, 128, 3), [10, 20, 30], dtype=np.uint8)
    tensor, _ = predictor.preprocess(frame)
    np.testing.assert_allclose(tensor[0, :, 0, 0], np.array([30, 20, 10]) / 255, atol=1e-7)
    prediction = predictor.predict(frame)
    assert len(prediction.detection.boxes) == 12
    assert any(box.label == 7 and box.class_name == '8' for box in prediction.detection.boxes)
    mask = prediction.segmentation.get_binary_mask()
    assert mask.shape == frame.shape[:2]
    assert not mask[:220].any() and mask[228:].all()
    np.testing.assert_allclose(
        next(box.xyxy for box in prediction.detection.boxes if box.class_name == '8'),
        [58, 126, 70, 146], atol=1e-4,
    )
    models = SimpleNamespace(
        draft_predictor=predictor,
        depth_estimator=StructureConstrainedVesselDraftDepthEstimation(class_names=class_names, min_confidence=.3),
    )
    result = FrameInferenceService(models).infer(
        frame, task_id='onnx-contract', source_filename='draft.png',
        task=TaskKind.DRAFT_ESTIMATION, observed_at=datetime.now(timezone.utc),
    )
    assert result.errors == []
    vessel = result.vessels[0]
    assert len(vessel.draft_marks) == 12 and vessel.water
    assert vessel.draft.success
    assert vessel.draft.depth_m == pytest.approx(3.2875, abs=.005)
    if class_names == NEW_CLASSES:
        assert 'M' not in {mark.class_name for mark in vessel.draft_marks}
        assert any(mark.class_name == '0' for mark in vessel.draft_marks)


@pytest.mark.parametrize(('classes', 'output_classes'), [(NEW_CLASSES, 9), (OLD_CLASSES, 8)])
def test_class_channel_mismatch_is_rejected_before_decoding(tmp_path, classes, output_classes):
    # Use only valid indices when creating the mismatched nine-class fixture.
    path = _write_package(tmp_path, NEW_CLASSES, logit_classes=output_classes)
    config_path = tmp_path / 'deploy_config.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['class_names'] = classes
    config['det_class_map'] = {name: index for index, name in enumerate(classes)}
    config['tasks']['num_det_classes'] = len(classes)
    config_path.write_text(yaml.safe_dump(config), encoding='utf-8')
    predictor = ONNXDraftFormerPredictor(path, device='cpu')
    with pytest.raises(ValueError, match='pred_logits must have shape'):
        predictor.predict(np.zeros((288, 128, 3), dtype=np.uint8))


def test_mask_must_include_detection_slots(tmp_path):
    predictor = ONNXDraftFormerPredictor(
        _write_package(tmp_path, NEW_CLASSES, mask_queries=1), device='cpu',
    )
    with pytest.raises(ValueError, match='pred_masks must have shape'):
        predictor.predict(np.zeros((288, 128, 3), dtype=np.uint8))


@pytest.mark.parametrize(('update', 'message'), [
    (lambda cfg: cfg['det_class_map'].update({'8': 8}), 'contiguous from 0'),
    (lambda cfg: cfg['tasks'].update(num_det_classes=9), 'configured detection class names'),
    (lambda cfg: cfg['tasks'].update(detection_queries=0), 'must be positive'),
])
def test_invalid_class_configuration_fails_explicitly(tmp_path, update, message):
    path = _write_package(tmp_path, NEW_CLASSES, config_update=update)
    with pytest.raises(ValueError, match=message):
        ONNXDraftFormerPredictor(path, device='cpu')
