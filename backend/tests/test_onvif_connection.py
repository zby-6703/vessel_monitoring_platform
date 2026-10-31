from dataclasses import replace
import asyncio
import threading
import xml.etree.ElementTree as ET

import httpx
import pytest

from app import live_capture
from app.config import get_settings


SOAP = "http://www.w3.org/2003/05/soap-envelope"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"


@pytest.fixture
def transport(monkeypatch):
    clients, requests = [], []
    state = {"fail": False}
    def handle(request):
        requests.append(request)
        if state["fail"]:
            state["fail"] = False
            raise httpx.ReadTimeout("simulated remote connection failure", request=request)
        return httpx.Response(200, content=f'<s:Envelope xmlns:s="{SOAP}"><s:Body><Response/></s:Body></s:Envelope>'.encode())
    original = httpx.Client
    def factory(**kwargs):
        assert kwargs["timeout"] == 5
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["limits"].keepalive_expiry == 30
        assert isinstance(kwargs["auth"], httpx.DigestAuth)
        client = original(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(live_capture.httpx, "Client", factory)
    settings = replace(get_settings(), camera_onvif_ptz_url="http://camera/ptz",
        camera_onvif_imaging_url="http://camera/imaging", camera_onvif_profile_token="profile",
        camera_onvif_username="user", camera_onvif_password="private-password")
    yield settings, clients, requests, state
    for client in clients:
        client.close()


def test_repeated_ptz_and_imaging_requests_reuse_one_client_but_fresh_wsse_nonce(transport):
    settings, clients, requests, _ = transport
    control = live_capture.OnvifControl()
    control._request(settings, control.PTZ, "GetStatus", "")
    control._request(settings, control.PTZ, "GetStatus", "")
    control._request(settings, control.IMAGING, "GetOptions", "")
    assert len(clients) == 1
    assert not clients[0].is_closed
    nonces = [ET.fromstring(request.content).findtext(f'.//{{{WSSE}}}Nonce') for request in requests]
    assert all(nonces) and len(set(nonces)) == 3
    assert all(b"private-password" not in request.content for request in requests)
    control.close()
    assert clients[0].is_closed


def test_transport_failure_discards_client_before_next_request(transport):
    settings, clients, _, state = transport
    control = live_capture.OnvifControl()
    control._request(settings, control.PTZ, "GetStatus", "")
    state["fail"] = True
    with pytest.raises(httpx.ReadTimeout):
        control._request(settings, control.PTZ, "GetStatus", "")
    assert clients[0].is_closed
    assert control._http_client is None
    control._request(settings, control.PTZ, "GetStatus", "")
    assert len(clients) == 2
    control.close()


def test_keepalive_only_reads_status_and_reuses_the_authenticated_connection(transport):
    settings, clients, requests, _ = transport
    control = live_capture.OnvifControl()
    # No native position is needed for keep-warm: it must still require the
    # camera's actual status response, and it must never move a lens.
    control._parse_zoom_status = lambda document: (.1, "IDLE", None)
    assert control.keep_control_alive(settings) is True
    assert control.keep_control_alive(settings) is True
    assert len(clients) == 1 and len(requests) == 2
    assert all(b"GetStatus" in request.content and b"AbsoluteMove" not in request.content for request in requests)
    control.close()


def test_keepalive_does_not_queue_behind_an_inflight_control_request(transport):
    settings, _, requests, _ = transport
    control = live_capture.OnvifControl()
    entered, release = threading.Event(), threading.Event()
    def hold():
        with control._http_lock:
            entered.set()
            assert release.wait(2)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(1)
        assert control.keep_control_alive(settings) is False
        assert requests == []
    finally:
        release.set()
        thread.join(2)
        control.close()


@pytest.mark.parametrize("changes", [{"camera_onvif_password": "changed-password"}, {"camera_onvif_ptz_url": "http://camera/new-ptz"}])
def test_changed_credentials_or_endpoints_dispose_previous_connection(transport, changes):
    settings, clients, _, _ = transport
    control = live_capture.OnvifControl()
    control._request(settings, control.PTZ, "GetStatus", "")
    control._request(replace(settings, **changes), control.PTZ, "GetStatus", "")
    assert len(clients) == 2
    assert clients[0].is_closed
    assert not clients[1].is_closed
    control.close()


def test_close_is_idempotent_and_next_explicit_request_can_reopen(transport):
    settings, clients, _, _ = transport
    control = live_capture.OnvifControl()
    control._request(settings, control.PTZ, "GetStatus", "")
    control.close()
    control.close()
    control._request(settings, control.PTZ, "GetStatus", "")
    assert len(clients) == 2 and clients[0].is_closed
    control.close()


def test_service_shutdown_closes_transport_after_motor_teardown(monkeypatch):
    from app import api
    events = []
    class Source:
        def stop(self): events.append("stop-source")
        def join(self): events.append("join-source")
    class Manager:
        def finish(self, **kwargs): events.append("finish-recording")
        def wait_for_optical_stop(self, **kwargs): events.append("motor-stopped"); return True
    class Control:
        def close(self): events.append("close-transport")
    monkeypatch.setattr(api, "local_playback", Source())
    monkeypatch.setattr(api, "camera_capture", Source())
    monkeypatch.setattr(api, "recording_manager", Manager())
    monkeypatch.setattr(api, "onvif_control", Control())
    asyncio.run(api.shutdown_realtime())
    assert events.index("close-transport") > events.index("motor-stopped")
