import json
import re

from conftest import event, put_minute

import handler



def ms(iso):
    from app.timeutil import parse_ts, to_ms
    return to_ms(parse_ts(iso))


def setup_minutes(ddb):
    # 10:20 DONE(2 断片)、10:21 FAILED、10:22 DONE(3 断片、途中から録画)、10:23 EMPTY、10:24 DONE(hls なし)
    put_minute(ddb, "cam-001", "2026-10-24T10:20:00Z", "DONE",
               segments=[(ms("2026-10-24T10:20:00Z"), ms("2026-10-24T10:21:00Z"))],
               hls=(1200, [(1200, 50000, 30000), (51200, 48000, 30000)]),
               files={"clip": "cam-001/2026/10/24/10/20/clip.mp4"})
    put_minute(ddb, "cam-001", "2026-10-24T10:21:00Z", "FAILED")
    put_minute(ddb, "cam-001", "2026-10-24T10:22:00Z", "DONE",
               segments=[(ms("2026-10-24T10:22:12.500Z"), ms("2026-10-24T10:23:00Z"))],
               hls=(1100, [(1100, 1000, 2000), (2100, 1500, 4250), (3600, 900, 1000)]))
    put_minute(ddb, "cam-001", "2026-10-24T10:23:00Z", "EMPTY")
    put_minute(ddb, "cam-001", "2026-10-24T10:24:00Z", "DONE")


def get(query):
    return handler.lambda_handler(event("GET", "/api/cameras/cam-001/vod.m3u8", query), None)


def test_playlist_format(ddb):
    setup_minutes(ddb)
    r = get({"from": "2026-10-24T10:20:30Z", "to": "2026-10-24T10:25:00Z"})
    assert r["statusCode"] == 200
    assert r["headers"]["content-type"] == "application/vnd.apple.mpegurl"
    text = r["body"]
    lines = text.strip().split("\n")
    assert lines[0] == "#EXTM3U"
    assert "#EXT-X-VERSION:7" in lines  # fMP4 + MAP の BYTERANGE は 6 以上が必要
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines
    assert lines[-1] == "#EXT-X-ENDLIST"
    assert text.endswith("\n")

    # 区間は 2 つ(FAILED / EMPTY / hls なしは入らない)、2 つ目の頭に DISCONTINUITY
    assert text.count("#EXT-X-MAP:") == 2
    assert text.count("#EXT-X-DISCONTINUITY\n") == 1
    assert text.count("#EXT-X-PROGRAM-DATE-TIME:") == 2
    assert '#EXT-X-MAP:URI="/media/cam-001/2026/10/24/10/20/clip.mp4",BYTERANGE="1200@0"' in lines
    # files.clip がなければ規約のキー
    assert '#EXT-X-MAP:URI="/media/cam-001/2026/10/24/10/22/clip.mp4",BYTERANGE="1100@0"' in lines
    assert "#EXT-X-PROGRAM-DATE-TIME:2026-10-24T10:20:00.000Z" in lines
    assert "#EXT-X-PROGRAM-DATE-TIME:2026-10-24T10:22:12.500Z" in lines

    # TARGETDURATION は EXTINF を丸めた最大値以上(RFC 8216 4.3.3.1)
    target = int(re.search(r"#EXT-X-TARGETDURATION:(\d+)", text).group(1))
    durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+),", text)]
    assert durs == [30.0, 30.0, 2.0, 4.25, 1.0]
    assert target == 30 and all(round(d) <= target for d in durs)

    # 各セグメントは EXTINF → BYTERANGE → URI の順
    for i, l in enumerate(lines):
        if l.startswith("#EXTINF:"):
            assert lines[i + 1].startswith("#EXT-X-BYTERANGE:")
            assert lines[i + 2].startswith("/media/") and lines[i + 2].endswith("clip.mp4")
    # MAP / PDT はそれぞれの区間の最初の EXTINF より前
    first_map = lines.index('#EXT-X-MAP:URI="/media/cam-001/2026/10/24/10/22/clip.mp4",BYTERANGE="1100@0"')
    assert lines[first_map - 1] == "#EXT-X-DISCONTINUITY"
    assert lines[first_map + 1].startswith("#EXT-X-PROGRAM-DATE-TIME:")
    assert lines[first_map + 2].startswith("#EXTINF:")
    # BYTERANGE は length@offset
    assert "#EXT-X-BYTERANGE:50000@1200" in lines
    assert "#EXT-X-BYTERANGE:1500@2100" in lines
    # ヘッダー部のタグは最初の MAP より前
    assert lines.index("#EXT-X-TARGETDURATION:30") < lines.index('#EXT-X-MAP:URI="/media/cam-001/2026/10/24/10/20/clip.mp4",BYTERANGE="1200@0"')


def test_playlist_range_limit(ddb):
    r = get({"from": "2026-10-24T00:00:00Z", "to": "2026-10-24T03:00:01Z"})
    assert r["statusCode"] == 400
    assert json.loads(r["body"])["error"]["code"] == "RANGE_TOO_LARGE"


def test_playlist_requires_range(ddb):
    r = get({"from": "2026-10-24T00:00:00Z"})
    assert r["statusCode"] == 400
    assert set(json.loads(r["body"])["error"]) == {"code", "message"}


def test_playlist_no_recording(ddb):
    setup_minutes(ddb)
    r = get({"from": "2026-10-24T10:21:00Z", "to": "2026-10-24T10:22:00Z"})
    assert r["statusCode"] == 404
    assert json.loads(r["body"])["error"]["code"] == "NO_RECORDING"


def test_playlist_minute_boundaries(ddb):
    setup_minutes(ddb)
    # to がちょうど 10:22:00 なら 10:22 の区間は入らない
    r = get({"from": "2026-10-24T10:20:00Z", "to": "2026-10-24T10:22:00Z"})
    assert r["body"].count("#EXT-X-MAP:") == 1
    # to が 10:22:00.001 なら入る
    r = get({"from": "2026-10-24T10:20:00Z", "to": "2026-10-24T10:22:00.001Z"})
    assert r["body"].count("#EXT-X-MAP:") == 2


def test_playlist_parses_with_m3u8_library(ddb):
    """第三者のパーサー(m3u8)で読んで、区間・MAP・PDT・BYTERANGE が意図どおりに解釈されること。"""
    import m3u8

    setup_minutes(ddb)
    text = get({"from": "2026-10-24T10:20:00Z", "to": "2026-10-24T10:25:00Z"})["body"]
    pl = m3u8.loads(text)
    assert pl.version == 7 and pl.is_endlist and pl.playlist_type == "vod"
    segs = pl.segments
    assert len(segs) == 5
    assert [s.byterange for s in segs] == ["50000@1200", "48000@51200", "1000@1100", "1500@2100", "900@3600"]
    assert [s.discontinuity for s in segs] == [False, False, True, False, False]
    assert segs[0].init_section.byterange == "1200@0"
    assert segs[2].init_section.uri.endswith("/10/22/clip.mp4")
    assert segs[2].program_date_time.isoformat() == "2026-10-24T10:22:12.500000+00:00"
    # PDT は区間内で引き継がれる(m3u8 が EXTINF から補完する)
    assert segs[3].current_program_date_time.isoformat() == "2026-10-24T10:22:14.500000+00:00"
