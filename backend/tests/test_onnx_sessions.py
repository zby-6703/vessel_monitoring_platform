from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from app.inference import onnx_backends


@pytest.mark.parametrize('device', ['cuda', 'cpu'])
def test_fp32_sessions_disable_tf32_on_cuda_before_loading_libraries(monkeypatch, device):
    captured = {}

    def register_libraries():
        import os
        assert os.environ['NVIDIA_TF32_OVERRIDE'] == '0'
        captured['cuda_libraries_loaded'] = True

    def create_session(path, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(get_providers=lambda: kwargs['providers'])

    monkeypatch.setenv('NVIDIA_TF32_OVERRIDE', '1')
    monkeypatch.setattr(onnx_backends, '_register_torch_cuda_libraries', register_libraries)
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(
        set_default_logger_severity_level=lambda level: None,
        SessionOptions=SimpleNamespace,
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=99),
        InferenceSession=create_session,
    ))
    onnx_backends._create_session(Path('model.onnx'), device)
    if device == 'cuda':
        assert captured['cuda_libraries_loaded']
        assert captured['provider_options'] == [{'use_tf32': '0'}, {}]
    else:
        assert 'cuda_libraries_loaded' not in captured
        assert captured['provider_options'] is None
