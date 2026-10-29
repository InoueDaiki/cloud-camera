"""操作画面の API(ソースの一覧と切り替え、WHIP の中継の入口)。プロセス内起動でも Docker でも動く。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from conftest import Target


def _url(target: Target, path: str) -> str:
    return f"http://{target.host}:{target.ui_port}{path}"


def _get(target: Target, path: str) -> dict:
    with urllib.request.urlopen(_url(target, path), timeout=10) as r:
        return json.load(r)


def _put_source(target: Target, index: int, source: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        _url(target, f"/api/cameras/{index}/source"), method="PUT",
        data=json.dumps({"source": source}).encode(), headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_index_page(target: Target):
    with urllib.request.urlopen(_url(target, "/"), timeout=10) as r:
        assert r.headers["Content-Type"].startswith("text/html")
        assert "WHIP" in r.read().decode()


def test_state(target: Target):
    st = _get(target, "/api/state")
    assert len(st["cameras"]) == target.cameras
    cam = st["cameras"][0]
    assert cam["index"] == 1 and cam["path"] == "cam1" and cam["onvif_port"] == target.onvif_port
    assert "testsrc" in st["sources"]["patterns"]
    assert isinstance(st["sources"]["files"], list)


def test_set_source_validates(target: Target):
    code, body = _put_source(target, 1, "nope")
    assert code == 400 and "nope" in body["error"]
    code, _ = _put_source(target, 1, "live:a/b")
    assert code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(_url(target, "/api/cameras/99/source"), method="PUT", data=b"{}"))
    assert e.value.code == 404


def test_set_source_roundtrip(target: Target):
    before = next(c for c in _get(target, "/api/state")["cameras"] if c["index"] == 1)["source"]
    try:
        code, body = _put_source(target, 1, "smptebars")
        assert code == 200 and body["source"] == "smptebars"
        assert next(c for c in _get(target, "/api/state")["cameras"] if c["index"] == 1)["source"] == "smptebars"
    finally:
        _put_source(target, 1, before)


def test_whip_rejects_bad_name(target: Target):
    req = urllib.request.Request(_url(target, "/whip/a%2Fb"), method="POST", data=b"v=0",
                                 headers={"Content-Type": "application/sdp"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code in (400, 404)
