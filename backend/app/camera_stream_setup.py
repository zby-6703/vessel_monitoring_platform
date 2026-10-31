"""Explicit, reversible Media 1 rate tuning for an isolated auxiliary encoder.

The caller must stop its stream and save ``prepare``'s plan before ``apply``.
This module never chooses a profile implicitly or changes resolution / codec.
"""
from __future__ import annotations

from copy import deepcopy
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from .camera_setup import CameraSetupError, MEDIA, SCHEMA, _Discovery, _hostname, _port


_RATE_FIELDS = ("FrameRateLimit", "EncodingInterval", "BitrateLimit")


def _shape(element):
    return (element.tag, tuple(sorted(element.attrib.items())), (element.text or "").strip(),
            tuple(_shape(child) for child in element))


def _number(element, path):
    try:
        return int(element.findtext(path, ""))
    except (TypeError, ValueError):
        raise CameraSetupError("Camera returned an invalid encoder configuration.") from None


def _summary(config):
    return {
        "encoder_token": config.attrib.get("token"),
        "encoding": config.findtext(f"{{{SCHEMA}}}Encoding"),
        "width": _number(config, f"{{{SCHEMA}}}Resolution/{{{SCHEMA}}}Width"),
        "height": _number(config, f"{{{SCHEMA}}}Resolution/{{{SCHEMA}}}Height"),
        **{name: _number(config, f"{{{SCHEMA}}}RateControl/{{{SCHEMA}}}{name}") for name in _RATE_FIELDS},
    }


class MediaStreamConfigurator:
    def __init__(self, host: str, username: str, password: str, *, media_url: str | None = None,
                 port: int = 80, timeout: float = 8, protected_profile_token: str = "MediaProfile000"):
        self.discovery = _Discovery(_hostname(host), username, password, _port(port, "HTTP port"), timeout, None)
        try:
            self.media_url = self.discovery.checked_url(media_url) if media_url else None
        except Exception:
            self.discovery.client.close()
            raise
        self.protected_profile_token = protected_profile_token

    def __enter__(self):
        try:
            if self.media_url is None:
                self.media_url = self.discovery.services()[MEDIA]
        except Exception:
            self.discovery.client.close()
            raise
        return self

    def __exit__(self, *_args):
        self.discovery.client.close()

    def _request(self, action, body=""):
        if self.media_url is None:
            raise CameraSetupError("Enter the stream configurator before use.")
        return self.discovery.request(self.media_url, MEDIA, action, body)

    def _binding(self, profile_token):
        if not isinstance(profile_token, str) or not profile_token or profile_token == self.protected_profile_token:
            raise CameraSetupError("Select an explicit auxiliary profile; the protected main profile cannot be changed.")
        profiles = self._request("GetProfiles").findall(f".//{{{MEDIA}}}Profiles")
        selected = [profile for profile in profiles if profile.attrib.get("token") == profile_token]
        if len(selected) != 1:
            raise CameraSetupError("Auxiliary media profile was not found uniquely.")
        encoder = selected[0].find(f"{{{SCHEMA}}}VideoEncoderConfiguration")
        token = encoder.attrib.get("token") if encoder is not None else None
        if not token:
            raise CameraSetupError("Selected profile has no encoder token.")
        users = [profile for profile in profiles
                 if (element := profile.find(f"{{{SCHEMA}}}VideoEncoderConfiguration")) is not None
                 and element.attrib.get("token") == token]
        if len(users) != 1:
            raise CameraSetupError("Selected encoder is shared by other profiles; refusing to change it.")
        if encoder.findtext(f"{{{SCHEMA}}}UseCount") not in {None, "1"}:
            raise CameraSetupError("Selected encoder is not exclusively used by the auxiliary profile.")
        return token

    def _read(self, token):
        document = self._request("GetVideoEncoderConfiguration", f"<t:ConfigurationToken>{escape(token)}</t:ConfigurationToken>")
        configs = document.findall(f".//{{{MEDIA}}}Configuration")
        if len(configs) != 1 or configs[0].attrib.get("token") != token:
            raise CameraSetupError("Camera returned another or ambiguous encoder configuration.")
        return deepcopy(configs[0])

    @staticmethod
    def _range(options, name, value, *, required=True):
        ranges = [item for item in options.iter() if item.tag.rsplit("}", 1)[-1] == name]
        if not ranges:
            if required:
                raise CameraSetupError(f"Camera did not declare {name}.")
            return
        for item in ranges:
            values = {child.tag.rsplit("}", 1)[-1]: child.text for child in item}
            try:
                minimum, maximum = int(values["Min"]), int(values["Max"])
            except (KeyError, TypeError, ValueError):
                raise CameraSetupError(f"Camera returned an invalid {name}.") from None
            if minimum > maximum or not minimum <= value <= maximum:
                raise CameraSetupError(f"Requested encoder value is outside {name}.")

    def prepare(self, profile_token: str, *, frame_rate: int = 12, bitrate: int = 1024):
        if type(frame_rate) is not int or not 1 <= frame_rate <= 120 or type(bitrate) is not int or not 32 <= bitrate <= 100000:
            raise CameraSetupError("Frame rate and bitrate must be valid positive integers.")
        token = self._binding(profile_token)
        original = self._read(token)
        before = _summary(original)
        if before["encoding"] != "H264":
            raise CameraSetupError("Only an existing H264 auxiliary encoder can be tuned.")
        document = self._request("GetVideoEncoderConfigurationOptions",
            f"<t:ConfigurationToken>{escape(token)}</t:ConfigurationToken><t:ProfileToken>{escape(profile_token)}</t:ProfileToken>")
        h264 = document.findall(f".//{{{SCHEMA}}}H264")
        if not h264:
            raise CameraSetupError("Camera did not declare H264 encoder options.")
        # Device extensions may repeat H264 with only BitrateRange. Validate
        # required fields in the primary options and bitrate wherever declared.
        primary = next((item for item in h264 if item.find(f"{{{SCHEMA}}}FrameRateRange") is not None), None)
        if primary is None:
            raise CameraSetupError("Camera did not declare H264 frame-rate options.")
        self._range(primary, "FrameRateRange", frame_rate)
        self._range(primary, "EncodingIntervalRange", 1)
        for item in h264:
            self._range(item, "BitrateRange", bitrate, required=False)
        resolutions = [(item.findtext(f"{{{SCHEMA}}}Width"), item.findtext(f"{{{SCHEMA}}}Height"))
                       for item in primary.findall(f"{{{SCHEMA}}}ResolutionsAvailable")]
        if (str(before["width"]), str(before["height"])) not in resolutions:
            raise CameraSetupError("Current resolution is not declared in H264 options; refusing to alter it.")
        target = deepcopy(original)
        for name, value in zip(_RATE_FIELDS, (frame_rate, 1, bitrate)):
            element = target.find(f"{{{SCHEMA}}}RateControl/{{{SCHEMA}}}{name}")
            if element is None:
                raise CameraSetupError("Camera encoder has incomplete rate-control fields.")
            element.text = str(value)
        return {"profile_token": profile_token, "encoder_token": token,
                "original_xml": ET.tostring(original, encoding="unicode"),
                "target_xml": ET.tostring(target, encoding="unicode"),
                "options_xml": ET.tostring(document, encoding="unicode"),
                "summary": {"profile_token": profile_token, "before": before, "target": _summary(target)}}

    def _validated_plan(self, plan):
        token = self._binding(plan["profile_token"])
        try:
            original, target = (ET.fromstring(plan[key]) for key in ("original_xml", "target_xml"))
        except (ET.ParseError, TypeError):
            raise CameraSetupError("Encoder plan contains invalid XML.") from None
        if any(config.tag != f"{{{MEDIA}}}Configuration" or config.attrib.get("token") != token
               for config in (original, target)) or plan.get("encoder_token") != token:
            raise CameraSetupError("Encoder plan does not match the current auxiliary profile.")
        normalized = deepcopy(target)
        for name in _RATE_FIELDS:
            path = f"{{{SCHEMA}}}RateControl/{{{SCHEMA}}}{name}"
            before, after = original.find(path), normalized.find(path)
            if before is None or after is None:
                raise CameraSetupError("Encoder plan has incomplete rate-control fields.")
            after.text = before.text
        if _shape(original) != _shape(normalized):
            raise CameraSetupError("Encoder plan changes fields other than the three allowed rate limits.")
        current = self._read(token)
        if _shape(current) not in {_shape(original), _shape(target)}:
            raise CameraSetupError("Encoder changed since preparation; refusing to overwrite concurrent changes.")
        return token, original, target

    def _write_verified(self, token, config):
        self._request("SetVideoEncoderConfiguration", ET.tostring(config, encoding="unicode") + "<t:ForcePersistence>true</t:ForcePersistence>")
        actual = self._read(token)
        if _shape(actual) != _shape(config):
            raise CameraSetupError("Encoder write was not verified exactly; retain the backup and inspect/restore it.")
        return _summary(actual)

    def apply(self, plan):
        token, _, target = self._validated_plan(plan)
        return self._write_verified(token, target)

    def restore(self, plan):
        token, original, _ = self._validated_plan(plan)
        return self._write_verified(token, original)
