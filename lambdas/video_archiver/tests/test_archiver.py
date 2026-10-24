import io
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws
from PIL import Image as PILImage

import archiver
from archiver import Archiver, Clip, build_clip, build_playlist, build_sprite, parse_master_playlist, parse_media_playlist
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


def _jpeg(color, size=(160, 90)):
    b = io.BytesIO()
    PILImage.new("RGB", size, color).save(b, format="JPEG")
    return b.getvalue()


def test_build_sprite_layout():
    start = T(10, 20)
    imgs = [
        kvs.Image(ts_ms=epoch_ms(start), content=_jpeg((255, 0, 0)), error=None),  # 0 秒 → 左上
        kvs.Image(ts_ms=epoch_ms(T(10, 20, 11)), content=_jpeg((0, 0, 255), (320, 180)), error=None),  # 11 秒 → 2 行目 2 列目(縮小)
        kvs.Image(ts_ms=epoch_ms(T(10, 20, 59)), content=_jpeg((0, 255, 0)), error=None),  # 59 秒 → 右下
        kvs.Image(ts_ms=epoch_ms(T(10, 20, 5)), content=None, error="NO_MEDIA"),
        kvs.Image(ts_ms=epoch_ms(T(10, 21, 0)), content=_jpeg((255, 255, 255)), error=None),  # 範囲外
        kvs.Image(ts_ms=epoch_ms(T(10, 20, 30)), content=b"broken", error=None),
    ]
    data, n = build_sprite(imgs, start)
    assert n == 3
    sheet = PILImage.open(io.BytesIO(data))
    assert sheet.size == (1600, 540)

    def px(slot):
        x, y = (slot % 10) * 160 + 80, (slot // 10) * 90 + 45
        return sheet.getpixel((x, y))

    def near(a, b):
        return all(abs(x - y) < 40 for x, y in zip(a, b))

    assert near(px(0), (255, 0, 0))
    assert near(px(11), (0, 0, 255))
    assert near(px(59), (0, 255, 0))
    assert near(px(5), (0, 0, 0))  # 画像がない秒は黒
    assert near(px(30), (0, 0, 0))


class FakeKvs:
    def __init__(self, frags, images=None, images_error=None):
        self.frags = frags
        self.images = images or []
        self.images_error = images_error
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

    def get_images(self, stream, start, end, **kw):
        self.calls.append(("images", kw))
        if self.images_error:
            raise self.images_error
        return self.images


@pytest.fixture
def s3():
    with mock_aws():
        c = boto3.client("s3", region_name="ap-northeast-1")
        c.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        yield c


def make(s3, fk):
    return Archiver(bucket=BUCKET, s3=s3, list_fragments=fk.list_fragments, clip_builder=fk.clip_builder,
                    get_images=fk.get_images)


def test_process_minute_empty(s3):
    fk = FakeKvs([])
    r = make(s3, fk).process_minute("cam-001", T(10, 20), T(10, 21))
    assert r.status == "EMPTY"
    assert r.attributes == {"segments": [], "fragment_count": 0, "bytes": 0}
    assert fk.calls[0][1] == "cc-cam-001"
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_process_minute_done(s3):
    frs = [frag(1, T(10, 20, 0)), frag(2, T(10, 20, 1)), frag(3, T(10, 20, 40))]
    imgs = [kvs.Image(ts_ms=epoch_ms(T(10, 20, s)), content=_jpeg((200, 200, 200)), error=None) for s in (0, 1, 40)]
    fk = FakeKvs(frs, imgs)
    r = make(s3, fk).process_minute("cam-001", T(10, 20), T(10, 21))
    assert r.status == "DONE"
    a = r.attributes
    p = "cam-001/2026/10/24/10/20/"
    assert a["files"] == {"clip": p + "clip.mp4", "playlist": p + "index.m3u8", "sprite": p + "sprite.jpg"}
    assert a["segments"] == [[epoch_ms(T(10, 20, 0)), epoch_ms(T(10, 20, 2))], [epoch_ms(T(10, 20, 40)), epoch_ms(T(10, 20, 41))]]
    assert a["hls"] == {"init_length": 8, "parts": [[8, 4, 1000], [12, 4, 1000], [16, 4, 1000]]}
    assert a["fragment_count"] == 3 and a["bytes"] == 20 and a["sprite_frames"] == 3
    assert ("images", {"interval_ms": 1000, "width": 160, "height": 90}) in fk.calls
    objs = {o["Key"]: o for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert set(objs) == set(a["files"].values())
    head = s3.head_object(Bucket=BUCKET, Key=p + "index.m3u8")
    assert head["ContentType"] == "application/vnd.apple.mpegurl"
    assert s3.head_object(Bucket=BUCKET, Key=p + "clip.mp4")["ContentType"] == "video/mp4"
    body = s3.get_object(Bucket=BUCKET, Key=p + "index.m3u8")["Body"].read().decode()
    assert "#EXT-X-DISCONTINUITY" in body


def test_process_minute_sprite_failure_still_archives(s3):
    fk = FakeKvs([frag(1, T(10, 20, 0))], images_error=RuntimeError("H.265 not supported"))
    r = make(s3, fk).process_minute("cam-001", T(10, 20), T(10, 21))
    assert r.status == "DONE"
    assert r.attributes["sprite_frames"] == 0
    assert "H.265" in r.attributes["sprite_error"]


def test_process_minute_clip_failure_raises(s3):
    fk = FakeKvs([frag(1, T(10, 20, 0))])

    def boom(*a):
        raise archiver.ArchiveError("hls")

    arch = Archiver(bucket=BUCKET, s3=s3, list_fragments=fk.list_fragments, clip_builder=boom, get_images=fk.get_images)
    with pytest.raises(archiver.ArchiveError):
        arch.process_minute("cam-001", T(10, 20), T(10, 21))
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


class NotFound(Exception):
    pass


NotFound.__name__ = "ResourceNotFoundException"


def test_images_fallback_per_segment(s3):
    calls = []

    def gi(stream, a, b, **kw):
        calls.append((a, b))
        if len(calls) == 1:
            raise NotFound("No fragments found in the stream for the GetImages request.")
        return [kvs.Image(ts_ms=epoch_ms(a), content=b"x", error=None)]

    arch = Archiver(bucket=BUCKET, s3=s3, get_images=gi)
    segs = [[epoch_ms(T(10, 20, 30, 979)), epoch_ms(T(10, 20, 40, 500))],
            [epoch_ms(T(10, 20, 50)), epoch_ms(T(10, 21, 0, 900))]]
    imgs, err = arch._images("s", T(10, 20), T(10, 21), segs)
    assert err is None
    # 1 回目は区間全体、失敗したら範囲ごと(開始は秒に切り上げ、終わりは区間の終わりで切る)
    assert calls == [(T(10, 20), T(10, 21)), (T(10, 20, 31), T(10, 20, 40, 500)), (T(10, 20, 50), T(10, 21))]
    assert len(imgs) == 2


def test_images_other_error_is_reported(s3):
    def gi(stream, a, b, **kw):
        raise RuntimeError("throttled")

    imgs, err = Archiver(bucket=BUCKET, s3=s3, get_images=gi)._images("s", T(10, 20), T(10, 21), [[0, 1]])
    assert imgs == [] and "throttled" in err
