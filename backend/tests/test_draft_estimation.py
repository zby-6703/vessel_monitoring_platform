import numpy as np
import pytest

from app.tracking import StructureConstrainedVesselDraftDepthEstimation
from runtime_support.algorithms.structure_constrained_vessel_draft_depth_estimation import (
    DetBox,
    GridPatternMatcher,
)


NEW_CLASSES = ['0', '1', '2', '3', '4', '5', '6', '8']
OLD_CLASSES = [*NEW_CLASSES, 'M']


def _character(name, x, y, normal):
    box = DetBox(name, x - 6, y - 10, x + 6, y + 10, .95)
    box._proj_main = y
    box._proj_normal = normal
    return box


@pytest.mark.parametrize(('integer', 'decimal', 'expected'), [
    ('4', '0', 4.0),
    ('4', 'M', 4.0),
    ('4', '2', 4.2),
    ('0', '2', .2),
    ('0', '0', None),
    ('M', '2', None),
    ('6', '2', None),
])
def test_complete_scale_accepts_merged_zero_only_in_its_correct_position(integer, decimal, expected):
    boxes = [_character(integer, 40, 40, -12), _character(decimal, 64, 40, 12)]
    assert GridPatternMatcher()._parse_cluster_value(boxes) == expected


def test_single_integer_is_not_promoted_to_a_complete_whole_metre_scale():
    assert GridPatternMatcher()._parse_cluster_value([_character('4', 40, 40, -12)]) is None


def _scale_boxes(zero_symbol):
    # Six equally spaced rows cross the 4.0 m boundary: 4.4 ... 3.4.
    rows = [('4', '4'), ('4', '2'), ('4', zero_symbol), ('3', '8'), ('3', '6'), ('3', '4')]
    return [
        _character(character, x, 40 + row * 32, normal)
        for row, characters in enumerate(rows)
        for character, x, normal in zip(characters, (40, 64), (-12, 12))
    ]


def test_merged_zero_and_legacy_m_have_identical_template_scores_and_scale_values():
    legacy_matcher = GridPatternMatcher()
    merged_matcher = GridPatternMatcher()
    legacy, legacy_debug = legacy_matcher.process(_scale_boxes('M'))
    merged, merged_debug = merged_matcher.process(_scale_boxes('0'))
    assert [scale.value for scale in merged] == pytest.approx([4.4, 4.2, 4., 3.8, 3.6, 3.4])
    assert [scale.value for scale in merged] == [scale.value for scale in legacy]
    assert merged_debug['best_match_score'] == pytest.approx(legacy_debug['best_match_score'])
    assert merged_debug['row_step'] == pytest.approx(legacy_debug['row_step'])
    assert legacy_matcher._parse_cluster_value(_scale_boxes('M')[4:6]) == 4.
    assert merged_matcher._parse_cluster_value(_scale_boxes('0')[4:6]) == 4.


@pytest.mark.parametrize(('class_names', 'zero_symbol'), [(NEW_CLASSES, '0'), (OLD_CLASSES, 'M')])
def test_eight_and_nine_class_models_estimate_the_same_depth_across_whole_metres(class_names, zero_symbol):
    characters = _scale_boxes(zero_symbol)
    boxes = np.array([[b.x1, b.y1, b.x2, b.y2] for b in characters], dtype=np.float32)
    labels = np.array([class_names.index(b.cls_name) for b in characters])
    mask = np.zeros((288, 128), dtype=np.uint8)
    mask[224:, :] = 1
    estimator = StructureConstrainedVesselDraftDepthEstimation(class_names=class_names, min_confidence=.3)
    result = estimator.estimate_with_details(boxes, labels, np.full(len(boxes), .95), mask)
    assert result.success
    # 0.2 m per 32 px, with the existing 6 px waterline compensation.
    assert result.depth == pytest.approx(3.2875, abs=.005)
    assert [scale.value for scale in result.scales] == pytest.approx([4.4, 4.2, 4., 3.8, 3.6, 3.4])
