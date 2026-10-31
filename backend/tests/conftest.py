from __future__ import annotations

import sys
from pathlib import Path
import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture(autouse=True)
def isolate_monitoring_events(tmp_path, monkeypatch):
    from app import monitoring_audit
    monkeypatch.setattr(monitoring_audit, "AUDIT_PATH", tmp_path / "monitoring-events.jsonl")

