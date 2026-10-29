from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

from dotenv import dotenv_values
import httpx
import pytest

from app import camera_setup as setup


HOST = "192.168.1.108"
SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"


def envelope(body):
    return (f'<s:Envelope xmlns:s="{setup.SOAP}" xmlns:d="{setup.DEVICE}" '
            f'xmlns:m="{setup.MEDIA}" xmlns:p="{setup.PTZ}" xmlns:tt="{setup.SCHEMA}">'
            f'<s:Body>{body}</s:Body></s:Envelope>')


def profile(token="main-real", width=1280, height=720, ptz=True):
    return (f'<m:Profiles token="{token}"><tt:Name>{token}</tt:Name>'
            '<tt:VideoSourceConfiguration token="source-config"><tt:SourceToken>source-real</tt:SourceToken></tt:VideoSourceConfiguration>'
            '<tt:VideoEncoderConfiguration token="encoder"><tt:Encoding>H264</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height></tt:Resolution>'
            '<tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit></tt:RateControl></tt:VideoEncoderConfiguration>'
            + ('<tt:PTZConfiguration token="ptz-real"/>' if ptz else '') + '</m:Profiles>')


@pytest.fixture
def camera(monkeypatch):
    clock = datetime.now(timezone.utc)
    bodies = {
        "GetSystemDateAndTime": '<d:GetSystemDateAndTimeResponse><tt:UTCDateTime>'
        f'<tt:Date><tt:Year>{clock.year}</tt:Year><tt:Month>{clock.month}</tt:Month><tt:Day>{clock.day}</tt:Day></tt:Date>'
        f'<tt:Time><tt:Hour>{clock.hour}</tt:Hour><tt:Minute>{clock.minute}</tt:Minute><tt:Second>{clock.second}</tt:Second></tt:Time>'
        '</tt:UTCDateTime></d:GetSystemDateAndTimeResponse>',
        "GetServices": '<d:GetServicesResponse>' + ''.join(
            f'<d:Service><d:Namespace>{namespace}</d:Namespace><d:XAddr>http://{HOST}/actual/{name}</d:XAddr></d:Service>'
            for namespace, name in ((setup.MEDIA, "media"), (setup.PTZ, "ptz"), (setup.IMAGING, "imaging"))) + '</d:GetServicesResponse>',
        "GetDeviceInformation": '<d:GetDeviceInformationResponse><d:Manufacturer>Camera</d:Manufacturer><d:Model>PTZ</d:Model><d:FirmwareVersion>1.0</d:FirmwareVersion></d:GetDeviceInformationResponse>',
        "GetProfiles": '<m:GetProfilesResponse>' + profile() + profile("sub-real", 704, 576) + '</m:GetProfilesResponse>',
        "GetVideoSources": '<m:GetVideoSourcesResponse><m:VideoSources token="source-real"/></m:GetVideoSourcesResponse>',
        "GetStreamUri": f'<m:GetStreamUriResponse><m:MediaUri><tt:Uri>rtsp://{HOST}:554/actual-stream?channel=1&amp;subtype=0</tt:Uri></m:MediaUri></m:GetStreamUriResponse>',
        "GetConfigurationOptions": f'<p:GetConfigurationOptionsResponse><tt:AbsoluteZoomPositionSpace><tt:URI>{SPACE}</tt:URI><tt:XRange><tt:Min>0</tt:Min><tt:Max>1</tt:Max></tt:XRange></tt:AbsoluteZoomPositionSpace></p:GetConfigurationOptionsResponse>',
        "GetStatus": f'<p:GetStatusResponse><p:PTZStatus><tt:Position><tt:Zoom x="0.0333333333333" space="{SPACE}"/></tt:Position><tt:MoveStatus><tt:Zoom>IDLE</tt:Zoom></tt:MoveStatus></p:PTZStatus></p:GetStatusResponse>',
        "GetCapabilities": '<d:GetCapabilitiesResponse><d:Capabilities/></d:GetCapabilitiesResponse>',
    }
    requests = []
    statuses = {}

    def handler(request):
        document = ET.fromstring(request.content)
        action = list(document.find(f'{{{setup.SOAP}}}Body'))[0].tag.rsplit('}', 1)[-1]
        requests.append((action, request))
        return httpx.Response(statuses.get(action, 200), content=envelope(bodies[action]).encode())

    client_type = httpx.Client
    monkeypatch.setattr(setup.httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    return bodies, requests, statuses


def test_discovery_uses_real_endpoints_and_encodes_private_credentials(camera):
    _, requests, _ = camera
    password = 'p:a@ss/?#&"'
    result = setup.discover_camera(HOST, "camera-user", password)
    private = result["env_values"]
    assert private["LIVE_CAPTURE_URL"] == f'rtsp://camera-user:{quote(password, safe="")}@{HOST}:554/actual-stream?channel=1&subtype=0'
    assert private["CAMERA_ONVIF_PTZ_URL"] == f"http://{HOST}/actual/ptz"
    assert private["CAMERA_ONVIF_PROFILE_TOKEN"] == "main-real"
    assert private["CAMERA_ONVIF_SOURCE_TOKEN"] == "source-real"
    assert private["CAMERA_ONVIF_PTZ_CONFIGURATION_TOKEN"] == "ptz-real"
    assert private["LIVE_CAPTURE_FPS_OVERRIDE"] == "25"
    assert "CAMERA_ONVIF_ZOOM_CALIBRATION" not in private
    assert result["summary"]["zoom_position"] == pytest.approx(1 / 30)
    assert result["summary"]["warnings"] == []
    assert password not in json.dumps(result["summary"])
    assert quote(password, safe="") not in json.dumps(result["summary"])
    assert all(request.url.host == HOST for _, request in requests)
    assert all(not name.startswith(("Set", "Move", "Absolute", "Continuous")) for name, _ in requests)
    assert b"PasswordDigest" in next(request.content for name, request in requests if name == "GetProfiles")


def test_equal_resolution_requires_explicit_real_profile(camera):
    bodies, _, _ = camera
    bodies["GetProfiles"] = '<m:GetProfilesResponse>' + profile() + profile("other-main") + '</m:GetProfilesResponse>'
    with pytest.raises(setup.CameraSetupError, match="specify profile_token"):
        setup.discover_camera(HOST, "user", "private-password")
    result = setup.discover_camera(HOST, "user", "private-password", profile_token="other-main")
    assert result["env_values"]["CAMERA_ONVIF_PROFILE_TOKEN"] == "other-main"


def test_an_unknown_profile_is_rejected(camera):
    with pytest.raises(setup.CameraSetupError, match="not found"):
        setup.discover_camera(HOST, "user", "private-password", profile_token="guessed-token")


def test_foreign_service_is_rejected_before_credentials_are_sent(camera):
    bodies, requests, _ = camera
    bodies["GetServices"] = bodies["GetServices"].replace(f"http://{HOST}/actual/ptz", "http://other-host/ptz")
    with pytest.raises(setup.CameraSetupError, match="another host"):
        setup.discover_camera(HOST, "user", "private-password")
    assert all(request.url.host == HOST for _, request in requests)


def test_foreign_stream_is_rejected(camera):
    bodies, _, _ = camera
    bodies["GetStreamUri"] = bodies["GetStreamUri"].replace(HOST, "other-host")
    with pytest.raises(setup.CameraSetupError, match="another host"):
        setup.discover_camera(HOST, "user", "private-password")


def test_faults_redact_plain_and_percent_encoded_passwords(camera):
    bodies, _, statuses = camera
    password = "secret:p@ss"
    bodies["GetProfiles"] = f'<s:Fault><s:Reason><s:Text>Rejected {password} rtsp://user:{quote(password, safe="")}@camera/private</s:Text></s:Reason></s:Fault>'
    statuses["GetProfiles"] = 500
    with pytest.raises(setup.CameraSetupError) as error:
        setup.discover_camera(HOST, "user", password)
    assert password not in str(error.value)
    assert quote(password, safe="") not in str(error.value)
    assert "user:" not in str(error.value)


def test_no_ptz_is_a_video_only_setup(camera):
    bodies, _, _ = camera
    bodies["GetServices"] = '<d:GetServicesResponse><d:Service>' + f'<d:Namespace>{setup.MEDIA}</d:Namespace><d:XAddr>http://{HOST}/actual/media</d:XAddr>' + '</d:Service></d:GetServicesResponse>'
    bodies["GetProfiles"] = '<m:GetProfilesResponse>' + profile(ptz=False) + '</m:GetProfilesResponse>'
    result = setup.discover_camera(HOST, "user", "private-password")
    assert result["env_values"]["CAMERA_ONVIF_PTZ_URL"] == ""
    assert result["summary"]["zoom_position"] is None
    assert result["summary"]["warnings"]


def test_digital_only_space_does_not_enable_optical_recording(camera):
    bodies, _, _ = camera
    bodies["GetConfigurationOptions"] = bodies["GetConfigurationOptions"].replace(SPACE, "http://camera/DigitalZoom/PositionSpace")
    result = setup.discover_camera(HOST, "user", "private-password")
    assert result["env_values"]["CAMERA_ONVIF_ZOOM_SPACE_URI"] == ""
    assert result["summary"]["warnings"]
    assert "CAMERA_ONVIF_ZOOM_CALIBRATION" not in result["env_values"]


@pytest.mark.parametrize("host,port", [("http://camera/path", 80), (HOST, 0), (HOST, 65536), (HOST, True)])
def test_invalid_destination_is_rejected_without_network(camera, host, port):
    _, requests, _ = camera
    with pytest.raises(setup.CameraSetupError):
        setup.discover_camera(host, "user", "private-password", port=port)
    assert requests == []


def test_duplicate_profile_tokens_are_rejected(camera):
    bodies, _, _ = camera
    bodies["GetProfiles"] = '<m:GetProfilesResponse>' + profile() + profile() + '</m:GetProfilesResponse>'
    with pytest.raises(setup.CameraSetupError, match="duplicate"):
        setup.discover_camera(HOST, "user", "private-password")


def test_local_credential_store_preserves_special_characters_and_existing_values(tmp_path, monkeypatch, capsys):
    script = Path(__file__).resolve().parents[2] / "scripts" / "configure-camera.py"
    spec = importlib.util.spec_from_file_location("configure_camera_local_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING_MODEL=keep-this\n", encoding="utf-8")
    state = tmp_path / "private-state"
    monkeypatch.setattr(module, "ENV_FILE", env_path)
    monkeypatch.setattr(module, "STATE_DIR", state)
    password = "a'quoted\\password:#@&"
    module.save_private_values({"CAMERA_ONVIF_PASSWORD": password})
    assert dotenv_values(env_path)["CAMERA_ONVIF_PASSWORD"] == password
    assert dotenv_values(env_path)["EXISTING_MODEL"] == "keep-this"
    module.save_private_values({"CAMERA_ONVIF_PROFILE_TOKEN": "actual-profile"})
    assert dotenv_values(env_path)["CAMERA_ONVIF_PASSWORD"] == password
    assert (state / "env-before-camera.bak").read_text() == "EXISTING_MODEL=keep-this\n"
    assert not (state / "env.tmp").exists()
    assert capsys.readouterr().out == ""
