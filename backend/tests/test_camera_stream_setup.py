from copy import deepcopy
import json
import xml.etree.ElementTree as ET

import httpx
import pytest

from app import camera_stream_setup as setup
from app.camera_setup import SOAP, MEDIA, SCHEMA, CameraSetupError


def config(token="002", rate=25, bitrate=2048):
    return ET.fromstring(f'<t:Configuration xmlns:t="{MEDIA}" xmlns:tt="{SCHEMA}" token="{token}">'
        '<tt:Name>SubStream2</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>'
        '<tt:Resolution><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:Resolution>'
        f'<tt:Quality>4.0</tt:Quality><tt:RateControl><tt:FrameRateLimit>{rate}</tt:FrameRateLimit>'
        f'<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>{bitrate}</tt:BitrateLimit></tt:RateControl>'
        '<tt:H264><tt:GovLength>50</tt:GovLength><tt:H264Profile>High</tt:H264Profile></tt:H264>'
        '<tt:Multicast><tt:Port>40016</tt:Port><tt:AutoStart>false</tt:AutoStart></tt:Multicast>'
        '<tt:SessionTimeout>PT60S</tt:SessionTimeout></t:Configuration>')


@pytest.fixture
def camera(monkeypatch):
    state = {"config": config(), "shared": False, "clamp": False}
    calls = []
    def handle(request):
        root = ET.fromstring(request.content)
        operation = list(root.find(f'{{{SOAP}}}Body'))[0]
        action = operation.tag.rsplit('}', 1)[-1]
        calls.append((action, operation))
        if action == 'GetProfiles':
            data = f'<t:GetProfilesResponse xmlns:t="{MEDIA}" xmlns:tt="{SCHEMA}">'
            for profile, encoder in [('MediaProfile000', '002' if state['shared'] else '000'), ('MediaProfile002', '002')]:
                data += f'<t:Profiles token="{profile}"><tt:VideoEncoderConfiguration token="{encoder}"><tt:UseCount>1</tt:UseCount></tt:VideoEncoderConfiguration></t:Profiles>'
            data += '</t:GetProfilesResponse>'
        elif action == 'GetVideoEncoderConfiguration':
            data = f'<t:GetVideoEncoderConfigurationResponse xmlns:t="{MEDIA}">{ET.tostring(state["config"],encoding="unicode")}</t:GetVideoEncoderConfigurationResponse>'
        elif action == 'GetVideoEncoderConfigurationOptions':
            data = f'<t:GetVideoEncoderConfigurationOptionsResponse xmlns:t="{MEDIA}" xmlns:tt="{SCHEMA}"><t:Options><tt:H264>'
            data += '<tt:ResolutionsAvailable><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:ResolutionsAvailable>'
            for name, low, high in [('FrameRateRange',1,25),('EncodingIntervalRange',1,4),('BitrateRange',128,4096)]:
                data += f'<tt:{name}><tt:Min>{low}</tt:Min><tt:Max>{high}</tt:Max></tt:{name}>'
            data += '</tt:H264></t:Options></t:GetVideoEncoderConfigurationOptionsResponse>'
        elif action == 'SetVideoEncoderConfiguration':
            state['config'] = deepcopy(operation.find(f'{{{MEDIA}}}Configuration'))
            if state['clamp']:
                state['config'].find(f'{{{SCHEMA}}}RateControl/{{{SCHEMA}}}FrameRateLimit').text = '10'
            data = f'<t:SetVideoEncoderConfigurationResponse xmlns:t="{MEDIA}"/>'
        else:
            raise AssertionError(action)
        return httpx.Response(200,content=f'<s:Envelope xmlns:s="{SOAP}"><s:Body>{data}</s:Body></s:Envelope>'.encode())
    client_type=httpx.Client
    monkeypatch.setattr(setup._Discovery.__init__.__globals__['httpx'], 'Client', lambda **kwargs:client_type(transport=httpx.MockTransport(handle),**kwargs))
    return state,calls


def client():
    return setup.MediaStreamConfigurator('192.168.1.108','admin','private-secret',media_url='http://192.168.1.108/onvif/media_service')


def test_prepare_is_read_only_preserves_native_fields_and_apply_restore_are_verified(camera):
    state,calls=camera
    original=deepcopy(state['config'])
    with client() as camera_client:
        plan=camera_client.prepare('MediaProfile002')
        assert not any(name.startswith('Set') for name,_ in calls)
        assert 'private-secret' not in json.dumps(plan)
        assert plan['summary']['target']['width']==1280
        assert plan['summary']['target']['height']==720
        assert camera_client.apply(plan)['FrameRateLimit']==12
        updated=deepcopy(state['config'])
        for name in ('FrameRateLimit','EncodingInterval','BitrateLimit'):
            path=f'{{{SCHEMA}}}RateControl/{{{SCHEMA}}}{name}'
            updated.find(path).text=original.find(path).text
        assert setup._shape(updated)==setup._shape(original)
        writes=[operation for name,operation in calls if name=='SetVideoEncoderConfiguration']
        assert writes[0].findtext(f'{{{MEDIA}}}ForcePersistence')=='true'
        assert camera_client.restore(plan)['FrameRateLimit']==25
        assert setup._shape(state['config'])==setup._shape(original)


def test_main_and_shared_encoder_are_rejected_before_any_write(camera):
    state,calls=camera
    with client() as camera_client:
        with pytest.raises(CameraSetupError,match='protected main'):
            camera_client.prepare('MediaProfile000')
        state['shared']=True
        with pytest.raises(CameraSetupError,match='shared'):
            camera_client.prepare('MediaProfile002')
    assert not any(name.startswith('Set') for name,_ in calls)


@pytest.mark.parametrize('kwargs',[{'frame_rate':26},{'bitrate':64},{'frame_rate':True}])
def test_requested_values_must_fit_declared_options(camera,kwargs):
    _,calls=camera
    with client() as camera_client,pytest.raises(CameraSetupError):
        camera_client.prepare('MediaProfile002',**kwargs)
    assert not any(name.startswith('Set') for name,_ in calls)


def test_plan_cannot_change_resolution_and_does_not_overwrite_concurrent_changes(camera):
    state,calls=camera
    with client() as camera_client:
        plan=camera_client.prepare('MediaProfile002')
        bad=dict(plan,target_xml=plan['target_xml'].replace('1280','640'))
        with pytest.raises(CameraSetupError,match='three allowed'):
            camera_client.apply(bad)
        state['config'].find(f'{{{SCHEMA}}}Quality').text='3.0'
        with pytest.raises(CameraSetupError,match='changed since'):
            camera_client.apply(plan)
    assert not any(name.startswith('Set') for name,_ in calls)


def test_accepted_write_without_exact_readback_is_not_claimed_as_success(camera):
    state,_=camera
    with client() as camera_client:
        plan=camera_client.prepare('MediaProfile002')
        state['clamp']=True
        with pytest.raises(CameraSetupError,match='not verified exactly'):
            camera_client.apply(plan)


def test_external_media_host_is_rejected_before_credentials_are_sent(camera):
    _,calls=camera
    with pytest.raises(CameraSetupError,match='another host'):
        setup.MediaStreamConfigurator('192.168.1.108','admin','private-secret',media_url='http://foreign.example/media')
    assert calls==[]
