"""main のコーデックの切り替え(H.265 / H.264)の単体テスト。ネットワークも FFmpeg も使わない。"""

from __future__ import annotations

import pytest

from onvif_emulator.config import H264_CRF_OFFSET, load_config, normalize_codec
from onvif_emulator.services import CameraServices, RequestContext
from onvif_emulator.soap import parse_request
from onvif_emulator.streams import ffmpeg_command

MEDIA1 = "http://www.onvif.org/ver10/media/wsdl"
MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"


def _call(svc: CameraServices, ns: str, op: str, inner: str = "") -> str:
    env = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Header>'
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<wsse:UsernameToken><wsse:Username>admin</wsse:Username><wsse:Password>admin</wsse:Password>"
        f'</wsse:UsernameToken></wsse:Security></s:Header><s:Body><x:{op} xmlns:x="{ns}">{inner}</x:{op}>'
        "</s:Body></s:Envelope>"
    )
    return svc.handle(parse_request(env.encode()), RequestContext(host="127.0.0.1", local_ip="127.0.0.1"))


@pytest.mark.parametrize("v, want", [("H265", "H265"), ("h.265", "H265"), ("HEVC", "H265"),
                                     ("h264", "H264"), ("H.264", "H264"), ("avc", "H264")])
def test_normalize_codec(v, want):
    assert normalize_codec(v) == want


def test_main_codec_default_is_h265_and_per_camera_override(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("cameras:\n  - {}\n  - {main_codec: h264}\n  - {}\n")
    cfg = load_config(y, env={"EMULATOR_CAM3_MAIN_CODEC": "H.264"})
    assert [c.main.codec for c in cfg.cameras] == ["H265", "H264", "H264"]
    assert all(c.sub.codec == "H264" for c in cfg.cameras)
    with pytest.raises(ValueError):
        load_config(env={"EMULATOR_MAIN_CODEC": "VP9"})


def test_crf_offset_for_h264():
    cfg = load_config(env={"EMULATOR_CAMERAS": "2", "EMULATOR_CAM2_MAIN_CODEC": "H264"})
    h265, h264 = cfg.cameras[0].main, cfg.cameras[1].main
    assert h265.quality == h264.quality == 60
    assert h265.crf == 28
    assert h264.crf == 28 - H264_CRF_OFFSET


def test_ffmpeg_command_h264_main(tmp_path):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_MAIN_CODEC": "H264", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    cam = cfg.cameras[0]
    cmd = ffmpeg_command(cfg, cam)
    assert "libx265" not in cmd and "-x265-params" not in cmd
    main = cmd[cmd.index("[main]"): cmd.index("[sub]")]
    assert main[main.index("-c:v") + 1] == "libx264"
    assert main[main.index("-crf") + 1] == str(cam.main.crf)
    assert main[main.index("-maxrate") + 1] == "1024k"
    assert main[main.index("-g") + 1] == "15" and main[main.index("-bf") + 1] == "0"
    assert "repeat-headers=1" in main[main.index("-x264-params") + 1]
    assert main[-2] == "rtsp://127.0.0.1:8554/cam1/main"


def test_onvif_reports_h264_main():
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_MAIN_CODEC": "H264", "EMULATOR_PTZ": "false"})
    svc = CameraServices(cfg, cfg.cameras[0])
    # Media2: main の Encoding が H264、Profile は High
    profiles = _call(svc, MEDIA2, "GetProfiles", "<x:Type>All</x:Type>")
    assert profiles.count("<tt:Encoding>H264</tt:Encoding>") == 2 and "H265" not in profiles
    opts = _call(svc, MEDIA2, "GetVideoEncoderConfigurationOptions",
                 "<x:ConfigurationToken>VideoEncoder_Main</x:ConfigurationToken>")
    assert "<tt:Encoding>H264</tt:Encoding>" in opts and 'ProfilesSupported="High"' in opts
    # Media1: H.264 になった main も VideoEncoderConfiguration を持つ
    m1 = _call(svc, MEDIA1, "GetProfiles")
    assert m1.count("<tt:Encoding>H264</tt:Encoding>") == 2
    conf = _call(svc, MEDIA1, "GetVideoEncoderConfiguration",
                 "<x:ConfigurationToken>VideoEncoder_Main</x:ConfigurationToken>")
    assert "<tt:Width>1280</tt:Width>" in conf


def test_onvif_default_main_is_h265_and_media1_hides_it():
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_PTZ": "false"})
    svc = CameraServices(cfg, cfg.cameras[0])
    profiles = _call(svc, MEDIA2, "GetProfiles", "<x:Type>All</x:Type>")
    assert "<tt:Encoding>H265</tt:Encoding>" in profiles
    m1 = _call(svc, MEDIA1, "GetVideoEncoderConfigurations")
    assert m1.count("<tt:Encoding>H264</tt:Encoding>") == 1 and "VideoEncoder_Main" not in m1
