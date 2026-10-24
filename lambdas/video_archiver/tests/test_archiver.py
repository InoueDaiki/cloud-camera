import io
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws
from PIL import Image as PILImage
from PIL import ImageStat

import archiver
from archiver import (Archiver, Clip, build_clip, build_playlist, build_sprite, codec_of_init, parse_master_playlist,
                      parse_media_playlist)
from common.tests.media import moof_offsets
from common import kvs
from common.timeutil import epoch_ms

BUCKET = "test-media"


def T(h, m, s=0, ms=0):
    return datetime(2026, 10, 24, h, m, s, ms * 1000, tzinfo=timezone.utc)


def frag(n, start, length=1000, size=100):
    return kvs.Fragment(number=str(n), producer_ms=epoch_ms(start), server_ms=epoch_ms(start), length_ms=length, size=size)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("STREAM_NAME_PREFIX", "cc-")


MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:CODECS="hvc1.1.6.L93.B0",RESOLUTION=1280x720,BANDWIDTH=1000000
getHLSMediaPlaylist.m3u8?SessionToken=abc
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:2
#EXT-X-MAP:URI="getMP4InitFragment.mp4?SessionToken=abc"
#EXTINF:1.000,
getMP4MediaFragment.mp4?FragmentNumber=100&SessionToken=abc
#EXTINF:1.000,
getMP4MediaFragment.mp4?FragmentNumber=101&SessionToken=abc
#EXTINF:1.000,
getMP4MediaFragment.mp4?FragmentNumber=102&SessionToken=abc
#EXT-X-ENDLIST
"""

BASE = "https://b-123.kinesisvideo.ap-northeast-1.amazonaws.com/hls/v1/"


def test_parse_playlists():
    media_url = parse_master_playlist(MASTER, BASE + "getHLSMasterPlaylist.m3u8?SessionToken=abc")
    assert media_url == BASE + "getHLSMediaPlaylist.m3u8?SessionToken=abc"
    m = parse_media_playlist(MEDIA, media_url)
    assert m.init_url == BASE + "getMP4InitFragment.mp4?SessionToken=abc"
    assert [n for n, _ in m.fragments] == ["100", "101", "102"]
    assert m.fragments[0][1].startswith(BASE + "getMP4MediaFragment.mp4?")
    # メディアプレイリストを直接渡された場合
    assert parse_master_playlist(MEDIA, "u") == "u"


def test_parse_media_playlist_requires_map():
    with pytest.raises(archiver.ArchiveError):
        parse_media_playlist("#EXTM3U\n#EXTINF:1,\nx.mp4?FragmentNumber=1\n", BASE)


def test_build_clip_filters_and_concatenates(monkeypatch):
    media = parse_media_playlist(MEDIA, BASE + "m.m3u8")
    monkeypatch.setattr(archiver, "open_hls", lambda stream, s, e: media)
    bodies = {"getMP4InitFragment.mp4": b"I" * 10}

    def fake_get(url):
        if "Init" in url:
            return bodies["getMP4InitFragment.mp4"]
        n = archiver._fragment_number(url)
        return n.encode() * 5  # "100" * 5 = 15 バイト

    monkeypatch.setattr(archiver, "http_get", fake_get)
    # 100 は前の区間に属する(ListFragments に出てこない)とする
    frs = [frag(101, T(10, 20, 0), 1000), frag(102, T(10, 20, 1), 1500)]
    clip = build_clip("s", T(10, 20), T(10, 21), frs)
    assert clip.init_length == 10
    assert clip.data == b"I" * 10 + b"101" * 5 + b"102" * 5
    assert clip.parts == [[10, 15, 1000], [25, 15, 1500]]
    assert [f.number for f in clip.fragments] == ["101", "102"]


def test_build_clip_raises_when_nothing_matches(monkeypatch):
    media = parse_media_playlist(MEDIA, BASE + "m.m3u8")
    monkeypatch.setattr(archiver, "open_hls", lambda stream, s, e: media)
    monkeypatch.setattr(archiver, "http_get", lambda url: b"x")
    with pytest.raises(archiver.ArchiveError):
        build_clip("s", T(10, 20), T(10, 21), [frag(999, T(10, 20))])


def test_build_playlist_with_gap():
    frs = [frag(1, T(10, 20, 0)), frag(2, T(10, 20, 1)), frag(3, T(10, 20, 30), 2000)]
    clip = Clip(data=b"", init_length=800, parts=[[800, 100, 1000], [900, 120, 1000], [1020, 300, 2000]], fragments=frs)
    pl = build_playlist(clip)
    lines = pl.splitlines()
    assert lines[0] == "#EXTM3U"
    assert "#EXT-X-TARGETDURATION:2" in lines
    assert '#EXT-X-MAP:URI="clip.mp4",BYTERANGE="800@0"' in lines
    assert lines.count("#EXT-X-DISCONTINUITY") == 1
    assert lines.count("clip.mp4") == 3
    assert "#EXT-X-BYTERANGE:120@900" in lines
    assert "#EXT-X-PROGRAM-DATE-TIME:2026-10-24T10:20:00.000Z" in lines
    assert "#EXT-X-PROGRAM-DATE-TIME:2026-10-24T10:20:30.000Z" in lines
    assert lines[-1] == "#EXT-X-ENDLIST"
    # DISCONTINUITY の直後に PROGRAM-DATE-TIME
    i = lines.index("#EXT-X-DISCONTINUITY")
    assert lines[i + 1].startswith("#EXT-X-PROGRAM-DATE-TIME")


def _clip_from_fmp4(data: bytes, producer_ms: list[int]) -> Clip:
    """make_fmp4 の出力を build_clip の結果と同じ形(init + 断片、parts、fragments)にする。"""
    offs = moof_offsets(data)
    ends = offs[1:] + [len(data)]
    frs = [kvs.Fragment(number=str(i), producer_ms=ms, server_ms=ms, length_ms=900, size=e - o)
           for i, (ms, o, e) in enumerate(zip(producer_ms, offs, ends))]
    parts = [[o, e - o, 1000] for o, e in zip(offs, ends)]
    return Clip(data=data, init_length=offs[0], parts=parts, fragments=frs)


def test_build_sprite_from_keyframes():
    """clip.mp4 のキーフレームを 1 秒 1 枚デコードし、枠(秒)の位置に並べる。GetImages は使わない。"""
    pytest.importorskip("av")
    from common.tests.media import BASE, START, level, make_fmp4

    data = make_fmp4(4)
    # 断片の開始(プロデューサー時刻)。3 本目の前に 10 秒の切れ目、4 本目は区間の外
    producer = [BASE, BASE + 1000, BASE + 12_000, BASE + 61_000]
    clip = _clip_from_fmp4(data, producer)
    start = datetime.fromtimestamp(START / 1000, timezone.utc)
    sp = build_sprite(clip, start, start + timedelta(minutes=1))
    assert sp.error is None
    assert sp.frames == 3
    assert sp.stats["decoded"] == 3 and sp.stats["skipped_outside"] == 1
    sheet = PILImage.open(io.BytesIO(sp.jpeg))
    assert sheet.size == (1600, 540)

    def mean(slot):
        x, y = (slot % 10) * 160, (slot // 10) * 90
        a = sheet.crop((x + 100, y + 20, x + 150, y + 70))  # 右寄り(左端の白い印を避ける)
        return ImageStat.Stat(a.convert("L")).mean[0]

    # 断片の開始 + 0.979 秒の位相 → コマ 0、1、12。各秒の明るさ(level)でどのキーフレームかを確かめる
    assert abs(mean(0) - level(0)) < 12
    assert abs(mean(1) - level(1)) < 12
    assert abs(mean(12) - level(2)) < 12
    assert mean(2) < 5 and mean(59) < 5  # キーフレームがない枠は黒


def test_build_sprite_broken_clip_returns_black():
    pytest.importorskip("av")
    clip = Clip(data=b"not mp4" * 10, init_length=7, parts=[[7, 63, 1000]], fragments=[frag(1, T(10, 20))])
    sp = build_sprite(clip, T(10, 20), T(10, 21))
    assert sp.frames == 0 and sp.error
    assert PILImage.open(io.BytesIO(sp.jpeg)).size == (1600, 540)


class FakeKvs:
    def __init__(self, frags, sprite_frames=0, sprite_error=None):
        self.frags = frags
        self.sprite_frames = sprite_frames
        self.sprite_error = sprite_error
        self.calls = []

    def list_fragments(self, stream, start, end):
        self.calls.append(("list", stream, start, end))
        return [f for f in self.frags if epoch_ms(start) <= f.producer_ms < epoch_ms(end)]

    def clip_builder(self, stream, start, end, frags):
        self.calls.append(("clip", stream))
        data = b"I" * 8
        parts = []
        for f in frags:
            parts.append([len(data), 4, f.length_ms])
            data += b"F" * 4
        return Clip(data=data, init_length=8, parts=parts, fragments=list(frags))

    def sprite_builder(self, clip, start, end):
        self.calls.append(("sprite", start, end, len(clip.fragments)))
        return archiver.Sprite(b"JPEG", self.sprite_frames, {"decode_ms": 1.0}, self.sprite_error)


@pytest.fixture
def s3():
    with mock_aws():
        c = boto3.client("s3", region_name="ap-northeast-1")
        c.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        yield c


def make(s3, fk):
    return Archiver(bucket=BUCKET, s3=s3, list_fragments=fk.list_fragments, clip_builder=fk.clip_builder,
                    sprite_builder=fk.sprite_builder)


def test_process_minute_empty(s3):
    fk = FakeKvs([])
    r = make(s3, fk).process_minute("cam-001", T(10, 20), T(10, 21))
    assert r.status == "EMPTY"
    assert r.attributes == {"segments": [], "fragment_count": 0, "bytes": 0}
    assert fk.calls[0][1] == "cc-cam-001"
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_process_minute_done(s3):
    frs = [frag(1, T(10, 20, 0)), frag(2, T(10, 20, 1)), frag(3, T(10, 20, 40))]
    fk = FakeKvs(frs, sprite_frames=3)
    r = make(s3, fk).process_minute("cam-001", T(10, 20), T(10, 21))
    assert r.status == "DONE"
    a = r.attributes
    p = "cam-001/2026/10/24/10/20/"
    assert a["files"] == {"clip": p + "clip.mp4", "playlist": p + "index.m3u8", "sprite": p + "sprite.jpg"}
    assert a["segments"] == [[epoch_ms(T(10, 20, 0)), epoch_ms(T(10, 20, 2))], [epoch_ms(T(10, 20, 40)), epoch_ms(T(10, 20, 41))]]
    assert a["hls"] == {"init_length": 8, "parts": [[8, 4, 1000], [12, 4, 1000], [16, 4, 1000]]}
    assert a["fragment_count"] == 3 and a["bytes"] == 20 and a["sprite_frames"] == 3
    assert ("sprite", T(10, 20), T(10, 21), 3) in fk.calls
    assert not any(c[0] == "images" for c in fk.calls)  # GetImages は呼ばない
    objs = {o["Key"]: o for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert set(objs) == set(a["files"].values())
    head = s3.head_object(Bucket=BUCKET, Key=p + "index.m3u8")
    assert head["ContentType"] == "application/vnd.apple.mpegurl"
    assert s3.head_object(Bucket=BUCKET, Key=p + "clip.mp4")["ContentType"] == "video/mp4"
    body = s3.get_object(Bucket=BUCKET, Key=p + "index.m3u8")["Body"].read().decode()
    assert "#EXT-X-DISCONTINUITY" in body


def test_process_minute_sprite_failure_still_archives(s3):
    fk = FakeKvs([frag(1, T(10, 20, 0))], sprite_error="InvalidDataError: broken")
    r = make(s3, fk).process_minute("cam-001", T(10, 20), T(10, 21))
    assert r.status == "DONE"
    assert r.attributes["sprite_frames"] == 0
    assert "broken" in r.attributes["sprite_error"]
    assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 3


def test_process_minute_clip_failure_raises(s3):
    fk = FakeKvs([frag(1, T(10, 20, 0))])

    def boom(*a):
        raise archiver.ArchiveError("hls")

    arch = Archiver(bucket=BUCKET, s3=s3, list_fragments=fk.list_fragments, clip_builder=boom,
                    sprite_builder=fk.sprite_builder)
    with pytest.raises(archiver.ArchiveError):
        arch.process_minute("cam-001", T(10, 20), T(10, 21))
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


# ---------------------------------------------------------------- 区間の途中でコーデックが変わる(docs/codec.md)

# KVS の HLS は、コーデックプライベートデータが変わったところで新しい EXT-X-MAP を出す(実測、2026-10-24)
MEDIA_SWITCH = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-TARGETDURATION:2
#EXT-X-MAP:URI="getMP4InitFragment.mp4?SessionToken=abc&TrackNumber=1"
#EXTINF:1.000,
getMP4MediaFragment.mp4?FragmentNumber=100&SessionToken=abc
#EXTINF:1.000,
getMP4MediaFragment.mp4?FragmentNumber=101&SessionToken=abc
#EXT-X-MAP:URI="getMP4InitFragment.mp4?SessionToken=abc&TrackNumber=1&SequenceNumber=3"
#EXTINF:1.000,
getMP4MediaFragment.mp4?FragmentNumber=200&SessionToken=abc
#EXT-X-ENDLIST
"""


def test_parse_media_playlist_sections():
    m = parse_media_playlist(MEDIA_SWITCH, BASE + "m.m3u8")
    assert [n for n, _ in m.fragments] == ["100", "101", "200"]
    assert len(m.sections) == 2
    assert m.sections[0][0].endswith("TrackNumber=1") and m.sections[1][0].endswith("SequenceNumber=3")
    assert [[n for n, _ in fr] for _, fr in m.sections] == [["100", "101"], ["200"]]
    assert m.init_url == m.sections[0][0]


def test_codec_of_init():
    pytest.importorskip("av")
    from common.tests.media import make_fmp4

    for codec, want in (("h265", "H265"), ("h264", "H264")):
        data = make_fmp4(1, codec=codec)
        assert codec_of_init(data[: moof_offsets(data)[0]]) == want
    assert codec_of_init(b"no init") is None


def _two_clips():
    a = Clip(data=b"I" * 8 + b"F" * 8, init_length=8, parts=[[8, 4, 1000], [12, 4, 1000]],
             fragments=[frag(1, T(10, 20, 0)), frag(2, T(10, 20, 1))], codec="H265")
    b = Clip(data=b"J" * 6 + b"G" * 4, init_length=6, parts=[[6, 4, 1000]], fragments=[frag(3, T(10, 20, 5))],
             codec="H264")
    return a, b


def test_build_playlist_switches_map():
    a, b = _two_clips()
    lines = build_playlist([a, b], ["clip.mp4", "clip.1.mp4"]).splitlines()
    maps = [l for l in lines if l.startswith("#EXT-X-MAP")]
    assert maps == ['#EXT-X-MAP:URI="clip.mp4",BYTERANGE="8@0"', '#EXT-X-MAP:URI="clip.1.mp4",BYTERANGE="6@0"']
    i = lines.index(maps[1])
    assert lines[i - 1] == "#EXT-X-DISCONTINUITY"
    assert lines[i + 1] == "#EXT-X-PROGRAM-DATE-TIME:2026-10-24T10:20:05.000Z"
    assert lines.count("clip.mp4") == 2 and lines.count("clip.1.mp4") == 1


def test_process_minute_with_codec_switch(s3):
    a, b = _two_clips()
    fk = FakeKvs([frag(1, T(10, 20, 0)), frag(2, T(10, 20, 1)), frag(3, T(10, 20, 5))])
    fk.clip_builder = lambda *args: [a, b]
    seen = []

    def sprite(clips, start, end):
        seen.append(clips)
        return archiver.Sprite(b"JPEG", 2, {})

    fk.sprite_builder = sprite
    r = make(s3, fk).process_minute("cam-001", T(10, 20), T(10, 21))
    at = r.attributes
    p = "cam-001/2026/10/24/10/20/"
    assert at["files"]["clip"] == p + "clip.mp4"
    assert at["files"]["clips"] == [p + "clip.mp4", p + "clip.1.mp4"]
    assert at["codecs"] == ["H265", "H264"]
    assert at["hls"]["parts"] == a.parts  # 1 つ目の部分(以前の形)
    secs = at["hls"]["sections"]
    assert [s["clip"] for s in secs] == at["files"]["clips"]
    assert [s["codec"] for s in secs] == ["H265", "H264"]
    assert secs[1]["init_length"] == 6 and secs[1]["start_ms"] == frag(3, T(10, 20, 5)).producer_ms
    assert at["fragment_count"] == 3 and at["bytes"] == 26
    assert seen == [[a, b]]
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert keys == {p + "clip.mp4", p + "clip.1.mp4", p + "index.m3u8", p + "sprite.jpg"}
    assert s3.get_object(Bucket=BUCKET, Key=p + "clip.1.mp4")["Body"].read() == b.data


def test_build_sprite_from_two_codecs():
    """H.265 の部分と H.264 の部分のキーフレームを 1 枚のシートにまとめる。"""
    pytest.importorskip("av")
    from common.tests.media import BASE as B
    from common.tests.media import START, make_fmp4

    h265 = _clip_from_fmp4(make_fmp4(2), [B, B + 1000])
    h264 = _clip_from_fmp4(make_fmp4(2, codec="h264"), [B + 10_000, B + 11_000])
    start = datetime.fromtimestamp(START / 1000, timezone.utc)
    sp = build_sprite([h265, h264], start, start + timedelta(minutes=1))
    assert sp.error is None and sp.frames == 4
    assert PILImage.open(io.BytesIO(sp.jpeg)).size == (1600, 540)


def test_build_clips_reopens_session_after_codec_switch(monkeypatch):
    """KVS は CPD が変わった後ろの断片を同じセッションでは返さないので、次の部分の最初の断片の時刻から開き直す。"""
    second = MEDIA_SWITCH.replace("SequenceNumber=3", "s2").split("#EXT-X-MAP")[0] + (
        '#EXT-X-MAP:URI="getMP4InitFragment.mp4?SessionToken=xyz&TrackNumber=1"\n'
        "#EXTINF:1.000,\ngetMP4MediaFragment.mp4?FragmentNumber=200&SessionToken=xyz\n#EXT-X-ENDLIST\n")
    sessions = []

    def fake_open(stream, s, e):
        sessions.append(s)
        return parse_media_playlist(MEDIA_SWITCH if len(sessions) == 1 else second, BASE + "m.m3u8")

    def fake_get(url):
        if "Init" in url:
            assert "SequenceNumber" not in url  # 1 つ目のセッションの 2 つ目の init は使わない
            return (b"S1" if "abc" in url else b"S2") * 4
        if "abc" in url:
            assert archiver._fragment_number(url) in ("100", "101")  # 1 つ目のセッションでは前の部分だけ取る
        return archiver._fragment_number(url).encode()

    monkeypatch.setattr(archiver, "open_hls", fake_open)
    monkeypatch.setattr(archiver, "http_get", fake_get)
    frs = [frag(100, T(10, 20, 0)), frag(101, T(10, 20, 1)), frag(200, T(10, 20, 8))]
    clips = archiver.build_clips("s", T(10, 20), T(10, 21), frs)
    assert sessions == [T(10, 20), T(10, 20, 8)]
    assert [[f.number for f in c.fragments] for c in clips] == [["100", "101"], ["200"]]
    assert clips[0].data == b"S1" * 4 + b"100101" and clips[1].data == b"S2" * 4 + b"200"
