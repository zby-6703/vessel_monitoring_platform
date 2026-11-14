import importlib.util
from pathlib import Path

import httpx
import pytest


def load_script():
    path = Path(__file__).resolve().parents[2] / "scripts/prepare-update.py"
    spec = importlib.util.spec_from_file_location("prepare_update", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_drains_only_active_sources_through_local_api(monkeypatch):
    module = load_script()
    requests = []
    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/": return httpx.Response(200, json={"service": "VesselSight Operations API"})
        if request.url.path == "/api/realtime/camera": return httpx.Response(200, json={"status": "idle"})
        if request.url.path == "/api/realtime/local-video": return httpx.Response(200, json={"status": "running"})
        if request.url.path == "/api/recording/status": return httpx.Response(200, json={"phase": "overview", "media_recording": False})
        return httpx.Response(200, json={"status": "stopped"})
    client = httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client)
    module.main()
    assert ("POST", "/api/realtime/local-video/stop") in requests
    assert ("POST", "/api/realtime/camera/stop") not in requests


def test_slow_existing_api_is_not_treated_as_a_stopped_platform(monkeypatch):
    module = load_script()
    def handler(request): raise httpx.ReadTimeout("busy API")
    client = httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(httpx.ReadTimeout): module.main()
