"""Read-only ONVIF discovery for private, local camera provisioning.

This module does not persist credentials, change camera settings, or move a lens.
Only ``env_values`` contains credentials; ``summary`` is suitable for local status
display. Returned service addresses must point to the explicitly selected camera.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx


SOAP = "http://www.w3.org/2003/05/soap-envelope"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"
MEDIA = "http://www.onvif.org/ver10/media/wsdl"
PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
IMAGING = "http://www.onvif.org/ver20/imaging/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"


class CameraSetupError(ValueError):
    """A discovery failure whose public message contains no credentials."""


def _redact(value: str, password: str, *, hide_urls: bool = True) -> str:
    text = str(value)
    if hide_urls:
        text = re.sub(r"\b(?:rtsps?|https?)://[^\s<>]+", "[camera endpoint]", text, flags=re.I)
    else:
        text = re.sub(r"(\b(?:rtsps?|https?)://)[^\s/@]+@", r"\1[redacted]@", text, flags=re.I)
    for secret in sorted({password, quote(password, safe="")}, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:1600]


def _public(value, password: str):
    if isinstance(value, str):
        return _redact(value, password, hide_urls=False)
    if isinstance(value, dict):
        return {key: _public(item, password) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item, password) for item in value]
    return value


def _hostname(value: str) -> str:
    host = str(value).strip().strip("[]")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        host = host.rstrip(".").lower()
        if not host or len(host) > 253 or any(
            not re.fullmatch(r"(?!-)[a-zA-Z0-9-]{1,63}(?<!-)", label)
            for label in host.split(".")
        ):
            raise CameraSetupError("Camera host must be an IP address or hostname, without a URL or port.") from None
        return host


def _port(value: int, label: str) -> int:
    if isinstance(value, bool):
        raise CameraSetupError(f"{label} must be an integer between 1 and 65535.")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise CameraSetupError(f"{label} must be an integer between 1 and 65535.") from None
    if number != value or not 1 <= number <= 65535:
        raise CameraSetupError(f"{label} must be an integer between 1 and 65535.")
    return number


def _authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class _Discovery:
    def __init__(self, host: str, username: str, password: str, port: int, timeout: float, device_url: str | None):
        self.host, self.username, self.password = host, username, password
        self.device_url = self.checked_url(device_url or f"http://{_authority(host, port)}/onvif/device_service")
        self.client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False,
                                   auth=httpx.DigestAuth(username, password))
        self.warnings: list[str] = []

    def checked_url(self, address: str, *, stream: bool = False) -> str:
        try:
            parsed = urlsplit(address)
            accepted = {"rtsp", "rtsps"} if stream else {"http", "https"}
            if parsed.scheme.lower() not in accepted or parsed.fragment or not parsed.hostname:
                raise ValueError
            if _hostname(parsed.hostname) != self.host:
                raise CameraSetupError("Camera returned an address for another host; refusing to send credentials there.")
            if not stream and (parsed.username is not None or parsed.password is not None):
                raise CameraSetupError("ONVIF service addresses must not contain user information.")
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
        except CameraSetupError:
            raise
        except (TypeError, ValueError):
            raise CameraSetupError("Camera returned an invalid service or stream address.") from None
        return address

    def request(self, endpoint: str, namespace: str, action: str, body: str = "", *, authenticated: bool = True) -> ET.Element:
        self.checked_url(endpoint)
        header = ""
        if authenticated:
            nonce = os.urandom(16)
            created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
            header = ('<wsse:Security s:mustUnderstand="1"><wsse:UsernameToken>'
                      f'<wsse:Username>{escape(self.username)}</wsse:Username>'
                      '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
                      f'{digest}</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
                      f'{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created>'
                      '</wsse:UsernameToken></wsse:Security>')
        envelope = ('<s:Envelope xmlns:s="' + SOAP + '" xmlns:t="' + namespace + '" xmlns:tt="' + SCHEMA + '" '
                    'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
                    'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
                    f'<s:Header>{header}</s:Header><s:Body><t:{action}>{body}</t:{action}></s:Body></s:Envelope>')
        try:
            kwargs = {} if authenticated else {"auth": None}
            response = self.client.post(endpoint, content=envelope.encode(), headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{namespace}/{action}"'}, **kwargs)
        except httpx.HTTPError as exc:
            raise CameraSetupError(f"ONVIF {action} connection failed: {_redact(str(exc), self.password)}") from None
        if 300 <= response.status_code < 400:
            raise CameraSetupError("Camera redirected an ONVIF request; redirects are disabled.")
        if response.status_code in {401, 403}:
            raise CameraSetupError("Camera rejected the ONVIF account. Check its password, permissions and clock.")
        if len(response.content) > 4 * 1024 * 1024:
            raise CameraSetupError("Camera returned an unexpectedly large ONVIF response.")
        try:
            document = ET.fromstring(response.content)
        except ET.ParseError:
            raise CameraSetupError(f"ONVIF {action} returned invalid XML (HTTP {response.status_code}).") from None
        fault = document.find(f".//{{{SOAP}}}Fault")
        if fault is not None:
            reason = fault.findtext(f".//{{{SOAP}}}Text") or "Operation not supported or refused."
            raise CameraSetupError(f"ONVIF {action}: {_redact(reason, self.password)}")
        if response.is_error:
            raise CameraSetupError(f"ONVIF {action} failed (HTTP {response.status_code}).")
        return document

    def optional(self, endpoint: str, namespace: str, action: str, body: str = "") -> ET.Element | None:
        try:
            return self.request(endpoint, namespace, action, body)
        except CameraSetupError as exc:
            self.warnings.append(str(exc))
            return None

    def services(self) -> dict[str, str]:
        result: dict[str, str] = {}
        document = self.optional(self.device_url, DEVICE, "GetServices", "<t:IncludeCapability>false</t:IncludeCapability>")
        if document is not None:
            for service in document.findall(f".//{{{DEVICE}}}Service"):
                namespace = service.findtext(f"{{{DEVICE}}}Namespace")
                address = service.findtext(f"{{{DEVICE}}}XAddr")
                if namespace in {MEDIA, PTZ, IMAGING} and address:
                    checked = self.checked_url(address.strip())
                    if namespace in result and result[namespace] != checked:
                        raise CameraSetupError("Camera advertised multiple addresses for the same ONVIF service.")
                    result[namespace] = checked
        if any(namespace not in result for namespace in (MEDIA, PTZ, IMAGING)):
            document = self.optional(self.device_url, DEVICE, "GetCapabilities", "<t:Category>All</t:Category>")
            if document is not None:
                for name, namespace in (("Media", MEDIA), ("PTZ", PTZ), ("Imaging", IMAGING)):
                    address = document.findtext(f".//{{{SCHEMA}}}{name}/{{{SCHEMA}}}XAddr")
                    if address and namespace not in result:
                        result[namespace] = self.checked_url(address.strip())
        if MEDIA not in result:
            detail = self.warnings[-1] if self.warnings else "No Media 1 service was advertised."
            raise CameraSetupError(f"Cannot discover the camera's ONVIF Media 1 service. {detail}")
        return result

    def clock_offset(self) -> float | None:
        before = time.time()
        try:
            document = self.request(self.device_url, DEVICE, "GetSystemDateAndTime", authenticated=False)
        except CameraSetupError:
            document = self.optional(self.device_url, DEVICE, "GetSystemDateAndTime")
        after = time.time()
        if document is None:
            return None
        clock = document.find(f".//{{{SCHEMA}}}UTCDateTime")
        if clock is None:
            self.warnings.append("Camera did not provide UTC time; its clock offset could not be checked.")
            return None
        try:
            values = [int(clock.findtext(f"{{{SCHEMA}}}{section}/{{{SCHEMA}}}{name}", ""))
                      for section, name in (("Date", "Year"), ("Date", "Month"), ("Date", "Day"),
                                            ("Time", "Hour"), ("Time", "Minute"), ("Time", "Second"))]
            offset = datetime(*values, tzinfo=timezone.utc).timestamp() - (before + after) / 2
        except (ValueError, TypeError, OverflowError):
            self.warnings.append("Camera returned invalid UTC time; its clock offset could not be checked.")
            return None
        if abs(offset) > 60:
            self.warnings.append("Camera clock differs from this computer by over 60 seconds; ONVIF authentication may fail.")
        return round(offset, 1)


def _profiles(document: ET.Element) -> list[dict]:
    result = []
    for element in document.findall(f".//{{{MEDIA}}}Profiles"):
        token = element.attrib.get("token", "")
        encoder = element.find(f"{{{SCHEMA}}}VideoEncoderConfiguration")
        if not token or encoder is None:
            continue
        try:
            width = int(encoder.findtext(f"{{{SCHEMA}}}Resolution/{{{SCHEMA}}}Width", "0"))
            height = int(encoder.findtext(f"{{{SCHEMA}}}Resolution/{{{SCHEMA}}}Height", "0"))
        except ValueError:
            width = height = 0
        ptz = element.find(f"{{{SCHEMA}}}PTZConfiguration")
        try:
            rate = float(encoder.findtext(f"{{{SCHEMA}}}RateControl/{{{SCHEMA}}}FrameRateLimit", "nan"))
        except ValueError:
            rate = math.nan
        result.append({"token": token, "name": element.findtext(f"{{{SCHEMA}}}Name", ""),
                       "width": max(0, width), "height": max(0, height), "ptz": ptz is not None,
                       "encoding": encoder.findtext(f"{{{SCHEMA}}}Encoding", ""),
                       "frame_rate_limit": rate if math.isfinite(rate) and 1 <= rate <= 120 else None,
                       "source_token": element.findtext(f"{{{SCHEMA}}}VideoSourceConfiguration/{{{SCHEMA}}}SourceToken", ""),
                       "ptz_configuration_token": ptz.attrib.get("token", "") if ptz is not None else ""})
    if len({profile["token"] for profile in result}) != len(result):
        raise CameraSetupError("Camera returned duplicate media profile tokens.")
    return result


def _select_profile(profiles: list[dict], token: str | None) -> dict:
    if token:
        candidates = [profile for profile in profiles if profile["token"] == token]
        if len(candidates) != 1:
            raise CameraSetupError("The requested media profile token was not found.")
        return dict(candidates[0])
    candidates = [profile for profile in profiles if profile["ptz"]] or profiles
    if not candidates:
        raise CameraSetupError("Camera returned no usable encoded video profiles.")
    largest = max(profile["width"] * profile["height"] for profile in candidates)
    candidates = [profile for profile in candidates if profile["width"] * profile["height"] == largest]
    if len(candidates) != 1:
        tokens = ", ".join(profile["token"] for profile in candidates)
        raise CameraSetupError(f"Several profiles have the same highest resolution ({tokens}); specify profile_token explicitly.")
    return dict(candidates[0])


def discover_camera(host: str, username: str, password: str, port: int = 80, rtsp_port: int = 554,
                    profile_token: str | None = None, *, device_service_url: str | None = None,
                    timeout: float = 8.0) -> dict:
    """Discover real endpoints and the largest PTZ-capable encoded video profile.

    ``rtsp_port`` is used only if the camera omits the port in its stream URI.
    A tied profile selection requires an explicit ``profile_token``. Missing PTZ
    or Imaging capabilities are reported as warnings, allowing video-only setup.
    No zoom calibration is inferred from a native ONVIF position.
    """
    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
        raise CameraSetupError("Enter the camera's ONVIF username and password locally.")
    if any(ord(character) < 32 for character in username + password):
        raise CameraSetupError("Camera credentials must not contain control characters.")
    discovery = None
    try:
        camera_host = _hostname(host)
        port, rtsp_port = _port(port, "ONVIF port"), _port(rtsp_port, "RTSP port")
        if not math.isfinite(timeout) or not .1 <= timeout <= 120:
            raise CameraSetupError("Camera timeout must be between 0.1 and 120 seconds.")
        discovery = _Discovery(camera_host, username.strip(), password, port, timeout, device_service_url)
        offset = discovery.clock_offset()
        services = discovery.services()
        device = discovery.optional(discovery.device_url, DEVICE, "GetDeviceInformation")
        device_info = {key: device.findtext(f".//{{{DEVICE}}}{name}", "") if device is not None else ""
                       for key, name in (("manufacturer", "Manufacturer"), ("model", "Model"), ("firmware_version", "FirmwareVersion"))}
        profiles = _profiles(discovery.request(services[MEDIA], MEDIA, "GetProfiles"))
        selected = _select_profile(profiles, profile_token)
        sources = discovery.optional(services[MEDIA], MEDIA, "GetVideoSources")
        if not selected["source_token"] and sources is not None:
            source_tokens = {element.attrib["token"] for element in sources.findall(f".//{{{MEDIA}}}VideoSources") if element.attrib.get("token")}
            if len(source_tokens) == 1:
                selected["source_token"] = source_tokens.pop()
        token_xml = escape(selected["token"])
        stream = discovery.request(services[MEDIA], MEDIA, "GetStreamUri",
            '<t:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>'
            f'</tt:Transport></t:StreamSetup><t:ProfileToken>{token_xml}</t:ProfileToken>')
        uri = stream.findtext(f".//{{{SCHEMA}}}Uri", "").strip()
        discovery.checked_url(uri, stream=True)
        parsed = urlsplit(uri)
        netloc = f"{quote(username.strip(), safe='')}:{quote(password, safe='')}@{_authority(camera_host, parsed.port or rtsp_port)}"
        private_uri = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
        ptz_config = selected["ptz_configuration_token"]
        spaces, zoom_position, zoom_movement = [], None, None
        if PTZ in services:
            if not ptz_config:
                compatible = discovery.optional(services[PTZ], PTZ, "GetCompatibleConfigurations", f"<t:ProfileToken>{token_xml}</t:ProfileToken>")
                if compatible is not None:
                    configs = {element.attrib["token"] for element in compatible.iter()
                               if element.tag.rsplit("}", 1)[-1] == "PTZConfiguration" and element.attrib.get("token")}
                    if len(configs) == 1:
                        ptz_config = configs.pop()
            if ptz_config:
                options = discovery.optional(services[PTZ], PTZ, "GetConfigurationOptions", f"<t:ConfigurationToken>{escape(ptz_config)}</t:ConfigurationToken>")
                if options is not None:
                    for space in options.findall(f".//{{{SCHEMA}}}AbsoluteZoomPositionSpace"):
                        space_uri = space.findtext(f"{{{SCHEMA}}}URI") or space.findtext(f"{{{SCHEMA}}}SpaceURI", "")
                        try:
                            low = float(space.findtext(f"{{{SCHEMA}}}XRange/{{{SCHEMA}}}Min", "nan"))
                            high = float(space.findtext(f"{{{SCHEMA}}}XRange/{{{SCHEMA}}}Max", "nan"))
                        except ValueError:
                            continue
                        if space_uri and math.isfinite(low) and math.isfinite(high) and high > low:
                            spaces.append({"uri": space_uri, "min": low, "max": high, "digital": "digital" in space_uri.lower()})
            status = discovery.optional(services[PTZ], PTZ, "GetStatus", f"<t:ProfileToken>{token_xml}</t:ProfileToken>")
            if status is not None:
                zoom = status.find(f".//{{{SCHEMA}}}Position/{{{SCHEMA}}}Zoom")
                try:
                    value = float(zoom.attrib.get("x", "nan")) if zoom is not None else math.nan
                    zoom_position = value if math.isfinite(value) else None
                except ValueError:
                    pass
                zoom_movement = status.findtext(f".//{{{SCHEMA}}}MoveStatus/{{{SCHEMA}}}Zoom")
        else:
            discovery.warnings.append("Camera did not advertise an ONVIF PTZ service; video setup does not enable lens control.")
        if IMAGING not in services or not selected["source_token"]:
            discovery.warnings.append("ONVIF Imaging service or video source token is missing; focus controls will be unavailable.")
        optical_spaces = [space for space in spaces if not space["digital"]]
        if not optical_spaces or zoom_position is None:
            discovery.warnings.append("Absolute zoom space or position feedback is missing; calibrated optical recording is not yet available.")
        elif len(optical_spaces) > 1:
            discovery.warnings.append("Multiple absolute zoom spaces were advertised; select the intended space explicitly.")
        env_values = {
            "LIVE_CAPTURE_URL": private_uri,
            "CAMERA_ONVIF_PTZ_URL": services.get(PTZ, ""),
            "CAMERA_ONVIF_IMAGING_URL": services.get(IMAGING, ""),
            "CAMERA_ONVIF_PROFILE_TOKEN": selected["token"],
            "CAMERA_ONVIF_SOURCE_TOKEN": selected["source_token"],
            "CAMERA_ONVIF_PTZ_CONFIGURATION_TOKEN": ptz_config,
            "CAMERA_ONVIF_ZOOM_SPACE_URI": optical_spaces[0]["uri"] if len(optical_spaces) == 1 else "",
            "CAMERA_ONVIF_USERNAME": username.strip(),
            "CAMERA_ONVIF_PASSWORD": password,
        }
        if selected["frame_rate_limit"] is not None:
            env_values["LIVE_CAPTURE_FPS_OVERRIDE"] = f"{selected['frame_rate_limit']:g}"
        summary = {"device": device_info, "profiles": profiles, "selected_profile": selected,
                   "absolute_zoom_spaces": spaces, "zoom_position": zoom_position,
                   "zoom_move_status": zoom_movement, "clock_offset_seconds": offset,
                   "warnings": discovery.warnings}
        return {"env_values": env_values, "summary": _public(summary, password)}
    except CameraSetupError as exc:
        raise CameraSetupError(_redact(str(exc), password)) from None
    except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
        raise CameraSetupError(f"Camera discovery failed: {_redact(str(exc), password)}") from None
    finally:
        if discovery is not None:
            discovery.client.close()
