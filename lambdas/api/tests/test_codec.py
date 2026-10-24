"""映像形式(H.265 / H.264)の切り替え: PUT /codec と、途中で切り替わった区間の vod.m3u8 / timeline(docs/codec.md)。"""

import json
from decimal import Decimal as D

import boto3
import pytest
from conftest import event

import handler


def ms(iso):
    from app.timeutil import parse_ts, to_ms
    return to_ms(parse_ts(iso))


@pytest.fixture
def iot(ddb, monkeypatch):
    monkeypatch.setenv("IOT_DATA_ENDPOINT", "")
    boto3.client("iot", region_name="ap-northeast-1").create_thing(thingName="test-gw-001")
    data = boto3.client("iot-data", region_name="ap-northeast-1")
    data.update_thing_shadow(thingName="test-gw-001", payload=json.dumps({"state": {
        "desired": {"cameras": {"cam-001": {"onvif_url": "http://emulator:8080/onvif/device_service",
                                            "username": "admin", "password": "admin", "streaming": True}}},
    }}).encode())
    return data


@pytest.mark.parametrize("given, want", [("H264", "H264"), ("h.265", "H265"), ("HEVC", "H265")])
def test_set_codec(iot, given, want):
    r = handler.lambda_handler(event("PUT", "/api/cameras/cam-001/codec", body={"codec": given}), None)
    assert r["statusCode"] == 200, r["body"]
    assert json.loads(r["body"])["codec"] == want
    shadow = json.loads(iot.get_thing_shadow(thingName="test-gw-001")["payload"].read())
    desired = shadow["state"]["desired"]["cameras"]["cam-001"]
    assert desired["codec"] == want and desired["streaming"] is True  # 他の設定は残る


@pytest.mark.parametrize("body", [{}, {"codec": "VP9"}, {"codec": 1}, None])
def test_set_codec_bad_body(iot, body):
    r = handler.lambda_handler(event("PUT", "/api/cameras/cam-001/codec", body=body), None)
    assert r["statusCode"] == 400


def test_playlist_and_timeline_with_codec_switch(ddb):
    """区間の途中で H.265 → H.264 に変わった区間は、部分ごとに DISCONTINUITY と EXT-X-MAP を切り替える。"""
    p = "cam-001/2026/10/24/10/30/"
    ddb.put_item(Item={
        "camera_id": "cam-001", "sk": "2026-10-24T10:30:00Z", "video_status": "DONE",
        "segments": [[D(ms("2026-10-24T10:30:00Z")), D(ms("2026-10-24T10:30:02Z"))],
                     [D(ms("2026-10-24T10:30:05Z")), D(ms("2026-10-24T10:30:06Z"))]],
        "files": {"clip": p + "clip.mp4", "clips": [p + "clip.mp4", p + "clip.1.mp4"],
                  "sprite": p + "sprite.jpg"},
        "codecs": ["H265", "H264"],
        "hls": {"init_length": D(800), "parts": [[D(800), D(10), D(1000)], [D(810), D(10), D(1000)]],
                "sections": [
                    {"clip": p + "clip.mp4", "init_length": D(800), "codec": "H265",
                     "start_ms": D(ms("2026-10-24T10:30:00Z")),
                     "parts": [[D(800), D(10), D(1000)], [D(810), D(10), D(1000)]]},
                    {"clip": p + "clip.1.mp4", "init_length": D(600), "codec": "H264",
                     "start_ms": D(ms("2026-10-24T10:30:05Z")), "parts": [[D(600), D(20), D(1000)]]},
                ]},
    })
    ddb.put_item(Item={"camera_id": "cam-001", "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T10:31:00Z"})
    q = {"from": "2026-10-24T10:30:00Z", "to": "2026-10-24T10:31:00Z"}
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/vod.m3u8", q), None)
    assert r["statusCode"] == 200, r["body"]
    lines = r["body"].strip().split("\n")
    maps = [l for l in lines if l.startswith("#EXT-X-MAP")]
    assert maps == [f'#EXT-X-MAP:URI="/media/{p}clip.mp4",BYTERANGE="800@0"',
                    f'#EXT-X-MAP:URI="/media/{p}clip.1.mp4",BYTERANGE="600@0"']
    i = lines.index(maps[1])
    assert lines[i - 1] == "#EXT-X-DISCONTINUITY"
    assert lines[i + 1] == "#EXT-X-PROGRAM-DATE-TIME:2026-10-24T10:30:05.000Z"
    assert lines[i + 4] == f"/media/{p}clip.1.mp4"
    assert "CODECS" not in r["body"]  # メディアプレイリスト(コーデックは各 init から)

    t = handler.lambda_handler(event("GET", "/api/cameras/cam-001/timeline", q), None)
    assert t["statusCode"] == 200, t["body"]
    minutes = json.loads(t["body"])["minutes"]
    assert minutes and minutes[0]["codecs"] == ["H265", "H264"]
