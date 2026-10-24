import base64
from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import ANY, Stubber

from common import kvs
from common.timeutil import epoch_ms, media_prefix


def T(h, m, s=0, ms=0):
    return datetime(2026, 10, 24, h, m, s, ms * 1000, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear():
    kvs.clear_cache()
    yield
    kvs.clear_cache()


def frag(n, start, length=1000, size=100):
    return kvs.Fragment(number=str(n), producer_ms=epoch_ms(start), server_ms=epoch_ms(start) + 500,
                        length_ms=length, size=size)


def test_stream_name(monkeypatch):
    monkeypatch.setenv("STREAM_NAME_PREFIX", "cloud-camera-")
    assert kvs.stream_name_for("cam-001") == "cloud-camera-cam-001"
    assert kvs.stream_name_for("cam-001", prefix="x-") == "x-cam-001"


def test_media_prefix():
    assert media_prefix("cam-001", T(10, 20)) == "cam-001/2026/10/24/10/20/"


def test_build_segments_joins_small_gaps():
    fr = [frag(1, T(10, 20, 0), 1000), frag(2, T(10, 20, 1, 400), 1000),  # 400ms の隙間 → つなぐ
          frag(3, T(10, 20, 10), 2000),  # 7.6s の隙間 → 分ける
          frag(4, T(10, 20, 12, 500), 1000)]  # ちょうど 500ms → つなぐ
    segs = kvs.build_segments(fr)
    # 隙間の中央値は 450ms だが、末尾の補正は最大 250ms
    assert segs == [[epoch_ms(T(10, 20, 0)), epoch_ms(T(10, 20, 2, 650))],
                    [epoch_ms(T(10, 20, 10)), epoch_ms(T(10, 20, 13, 750))]]
    assert kvs.build_segments([]) == []


def test_effective_durations_adds_last_frame():
    # 実測と同じ形: 1 秒ごとの断片で長さ 933ms(最後の 1 フレーム 67ms ぶん短い)
    fr = [frag(i, T(10, 20, i, 979), 933) for i in range(3)] + [frag(9, T(10, 20, 30, 0), 933)]
    assert kvs.effective_durations(fr) == [1000, 1000, 1000, 1000]
    segs = kvs.build_segments(fr)
    assert segs == [[epoch_ms(T(10, 20, 0, 979)), epoch_ms(T(10, 20, 3, 979))],
                    [epoch_ms(T(10, 20, 30)), epoch_ms(T(10, 20, 31))]]
    assert kvs.effective_durations([frag(1, T(10, 20), 933)]) == [933]


def _stub_endpoint(monkeypatch, endpoints):
    """kinesisvideo クライアントを Stubber で差し替える。"""
    c = boto3.client("kinesisvideo", region_name="ap-northeast-1")
    st = Stubber(c)
    for ep in endpoints:
        st.add_response("get_data_endpoint", {"DataEndpoint": ep}, {"StreamName": ANY, "APIName": ANY})
    st.activate()
    monkeypatch.setattr(kvs, "_kv_client", c)
    return st


def test_endpoint_cache(monkeypatch):
    st = _stub_endpoint(monkeypatch, ["https://a.example.com", "https://b.example.com"])
    assert kvs.get_data_endpoint("s1", kvs.LIST_FRAGMENTS) == "https://a.example.com"
    assert kvs.get_data_endpoint("s1", kvs.LIST_FRAGMENTS) == "https://a.example.com"  # キャッシュ
    assert kvs.get_data_endpoint("s1", kvs.GET_IMAGES) == "https://b.example.com"
    st.assert_no_pending_responses()
    kvs.invalidate_endpoint("s1")
    with pytest.raises(Exception):
        kvs.get_data_endpoint("s1", kvs.LIST_FRAGMENTS)  # 再取得しようとして Stubber の応答切れ


def test_endpoint_cache_ttl(monkeypatch):
    _stub_endpoint(monkeypatch, ["https://a.example.com", "https://c.example.com"])
    now = [1000.0]
    monkeypatch.setattr(kvs.time, "monotonic", lambda: now[0])
    assert kvs.get_data_endpoint("s1", kvs.GET_CLIP) == "https://a.example.com"
    now[0] += kvs.ENDPOINT_TTL_SECONDS + 1
    assert kvs.get_data_endpoint("s1", kvs.GET_CLIP) == "https://c.example.com"


def _archived(monkeypatch):
    _stub_endpoint(monkeypatch, ["https://kvs.example.com"])
    c = boto3.client("kinesis-video-archived-media", region_name="ap-northeast-1",
                     endpoint_url="https://kvs.example.com")
    st = Stubber(c)
    kvs._clients["https://kvs.example.com"] = c
    return st


def test_list_fragments_filters_sorts_and_pages(monkeypatch):
    st = _archived(monkeypatch)

    def f(n, ts, length=1000):
        return {"FragmentNumber": str(n), "FragmentSizeInBytes": 10, "ProducerTimestamp": ts,
                "ServerTimestamp": ts, "FragmentLengthInMilliseconds": length}

    st.add_response("list_fragments", {
        "Fragments": [f(3, T(10, 20, 2)), f(1, T(10, 19, 59, 500)), f(2, T(10, 20, 0))],
        "NextToken": "tok"})
    st.add_response("list_fragments", {"Fragments": [f(4, T(10, 21, 0)), f(5, T(10, 20, 59, 999)), f(3, T(10, 20, 2))]},
                    {"StreamName": "s", "NextToken": "tok", "MaxResults": 1000})
    st.activate()
    got = kvs.list_fragments("s", T(10, 20), T(10, 21))
    # 開始時刻が [10:20, 10:21) の断片だけ、時刻順、重複なし
    assert [x.number for x in got] == ["2", "3", "5"]
    assert got[0].producer_ms == epoch_ms(T(10, 20)) and got[0].length_ms == 1000
    st.assert_no_pending_responses()


def test_list_fragments_server_timestamp(monkeypatch):
    st = _archived(monkeypatch)
    st.add_response("list_fragments", {"Fragments": [
        {"FragmentNumber": "1", "FragmentSizeInBytes": 1, "ProducerTimestamp": T(10, 0), "ServerTimestamp": T(10, 20, 5),
         "FragmentLengthInMilliseconds": 1000},
        {"FragmentNumber": "2", "FragmentSizeInBytes": 1, "ProducerTimestamp": T(10, 20, 5), "ServerTimestamp": T(10, 25),
         "FragmentLengthInMilliseconds": 1000}]},
        {"StreamName": "s", "MaxResults": 1000, "FragmentSelector": {
            "FragmentSelectorType": "SERVER_TIMESTAMP", "TimestampRange": {"StartTimestamp": T(10, 20), "EndTimestamp": T(10, 21)}}})
    st.activate()
    got = kvs.list_fragments("s", T(10, 20), T(10, 21), selector="SERVER_TIMESTAMP")
    assert [f.number for f in got] == ["1"]  # サーバー時刻で絞り込む


def _lf(n, producer, size=10, server=None, length=1000):
    return {"FragmentNumber": str(n), "FragmentSizeInBytes": size, "ProducerTimestamp": producer,
            "ServerTimestamp": server or producer, "FragmentLengthInMilliseconds": length}


def test_list_fragments_dedupes_resent_fragments(monkeypatch):
    # 回線断からの復旧時の再送: 同じ ProducerTimestamp の断片が 2 つ(番号が違う)入る
    st = _archived(monkeypatch)
    st.add_response("list_fragments", {"Fragments": [
        _lf(10, T(10, 20, 1), size=500), _lf(11, T(10, 20, 2), size=500),
        _lf(20, T(10, 20, 2), size=500, server=T(10, 25)),  # 再送(同じ大きさ)→ 先に届いた 11 を残す
        _lf(12, T(10, 20, 3), size=100),  # 途中で切れた断片
        _lf(21, T(10, 20, 3), size=500, server=T(10, 25)),  # 再送(大きい方を残す)
    ]})
    st.activate()
    got = kvs.list_fragments("s", T(10, 20), T(10, 21))
    assert [g.number for g in got] == ["10", "11", "21"]


def test_get_images_dedupes_same_timestamp(monkeypatch):
    st = _archived(monkeypatch)
    b64 = base64.b64encode(b"x").decode()
    st.add_response("get_images", {"Images": [
        {"TimeStamp": T(10, 20, 1), "Error": "NO_MEDIA"}, {"TimeStamp": T(10, 20, 1), "ImageContent": b64},
        {"TimeStamp": T(10, 20, 2), "ImageContent": b64}, {"TimeStamp": T(10, 20, 2), "ImageContent": b64}]})
    st.activate()
    got = kvs.get_images("s", T(10, 20), T(10, 21))
    assert [g.ts_ms for g in got] == [epoch_ms(T(10, 20, 1)), epoch_ms(T(10, 20, 2))]
    assert got[0].content == b"x"


def test_fragment_cache(monkeypatch):
    st = _archived(monkeypatch)
    st.activate()  # 応答なし: API を呼んだら失敗する
    frs = [frag(1, T(10, 20, 1)), frag(2, T(10, 21, 1)), frag(3, T(10, 22, 1))]
    kvs.set_fragment_cache("s", T(10, 20), T(10, 23), frs)
    assert [f.number for f in kvs.list_fragments("s", T(10, 21), T(10, 22))] == ["2"]
    assert kvs.list_fragments("s", T(10, 22), T(10, 23))[0].number == "3"
    # 範囲外・別のストリーム・サーバー時刻はキャッシュを使わない
    for args, kw in [(("s", T(10, 19), T(10, 21)), {}), (("other", T(10, 21), T(10, 22)), {}),
                     (("s", T(10, 21), T(10, 22)), {"selector": "SERVER_TIMESTAMP"})]:
        with pytest.raises(Exception):
            kvs.list_fragments(*args, **kw)
    kvs.clear_fragment_cache("s")
    with pytest.raises(Exception):
        kvs.list_fragments("s", T(10, 21), T(10, 22))


def test_watermark_ms():
    assert kvs.watermark_ms([]) is None
    assert kvs.watermark_ms([frag(2, T(10, 20, 5), 933), frag(1, T(10, 20, 1))]) == epoch_ms(T(10, 20, 5)) + 933


def test_get_images_pages_and_decodes(monkeypatch):
    st = _archived(monkeypatch)
    jpg = b"\xff\xd8jpeg"
    b64 = base64.b64encode(jpg).decode()
    st.add_response("get_images", {"Images": [{"TimeStamp": T(10, 20, 1), "ImageContent": b64}], "NextToken": "n"})
    st.add_response("get_images", {"Images": [{"TimeStamp": T(10, 20, 0), "ImageContent": b64},
                                              {"TimeStamp": T(10, 20, 2), "Error": "NO_MEDIA"}]})
    st.activate()
    got = kvs.get_images("s", T(10, 20), T(10, 21), width=160, height=90)
    assert [g.ts_ms for g in got] == [epoch_ms(T(10, 20, s)) for s in (0, 1, 2)]
    assert got[0].content == jpg and got[2].content is None and got[2].error == "NO_MEDIA"
    st.assert_no_pending_responses()


def test_minute_ready_is_deprecated_and_always_ready():
    # 互換のための残骸。判定は run_pipeline のウォーターマークが行う
    assert kvs.minute_ready("s", T(10, 21), T(10, 21, 1), quiet_seconds=30, max_wait_seconds=600) == (True, "watermark")


def test_endpoint_export_import(monkeypatch):
    """別の実行環境の起動が保存したエンドポイントを使い、GetDataEndpoint を呼ばない。"""
    _stub_endpoint(monkeypatch, ["https://a.example.com"])
    assert kvs.get_data_endpoint("s1", kvs.LIST_FRAGMENTS) == "https://a.example.com"
    saved = kvs.export_endpoints("s1")
    assert list(saved) == [kvs.LIST_FRAGMENTS]
    assert saved[kvs.LIST_FRAGMENTS][0] == "https://a.example.com"
    assert kvs.export_endpoints("other") == {}

    kvs.clear_cache()  # 別の実行環境に見立てる
    st = _stub_endpoint(monkeypatch, [])  # 呼ばれたら Stubber の応答切れで失敗する
    kvs.import_endpoints("s1", saved)
    assert kvs.get_data_endpoint("s1", kvs.LIST_FRAGMENTS) == "https://a.example.com"
    st.assert_no_pending_responses()


def test_endpoint_import_ignores_expired_and_garbage(monkeypatch):
    st = _stub_endpoint(monkeypatch, ["https://fresh.example.com"])
    old = kvs.time.time() - kvs.ENDPOINT_TTL_SECONDS - 1
    kvs.import_endpoints("s1", {kvs.LIST_FRAGMENTS: ["https://old.example.com", old],
                                kvs.GET_IMAGES: "broken", kvs.GET_CLIP: ["http://plain.example.com", kvs.time.time()]})
    kvs.import_endpoints("s1", None)
    assert kvs.export_endpoints("s1") == {}
    assert kvs.get_data_endpoint("s1", kvs.LIST_FRAGMENTS) == "https://fresh.example.com"
    st.assert_no_pending_responses()
    # 手元の方が新しければ上書きしない
    kvs.import_endpoints("s1", {kvs.LIST_FRAGMENTS: ["https://older.example.com", kvs.time.time() - 10]})
    assert kvs.get_data_endpoint("s1", kvs.LIST_FRAGMENTS) == "https://fresh.example.com"
