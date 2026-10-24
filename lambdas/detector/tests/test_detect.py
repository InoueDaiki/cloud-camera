"""前処理・後処理と、既知の画像(darknet の person.jpg)で person が検出されること。"""

import os

import numpy as np
import pytest
from PIL import Image

from conftest import FIXTURES, MODELS
from detect import Detector, Frame, input_size_for, nms, postprocess, preprocess


def test_input_size_for_16_9():
    assert input_size_for(1920, 1080, 416) == (256, 416)
    assert input_size_for(416, 234, 416) == (256, 416)
    assert input_size_for(640, 360, 640) == (384, 640)
    assert input_size_for(4, 3, 320) == (256, 320)


def test_preprocess_letterbox_bgr():
    img = Image.new("RGB", (832, 468), (255, 0, 0))  # 赤
    f = preprocess(img, (256, 416))
    assert f.tensor.shape == (3, 256, 416)
    assert f.tensor.dtype == np.float32
    assert f.ratio == pytest.approx(0.5)
    # BGR の順: 赤は channel 2
    assert f.tensor[2, 0, 0] == 255 and f.tensor[0, 0, 0] == 0
    # 下の余白は 114
    assert (f.tensor[:, 240:, :] == 114).all()


def test_nms_keeps_best_and_separate():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 10, 10], [20, 20, 30, 30]], dtype=np.float32)
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
    assert nms(boxes, scores, 0.5) == [0, 2]


def test_postprocess_decodes_and_normalizes():
    hw = (256, 416)
    n_anchors = sum((hw[0] // s) * (hw[1] // s) for s in (8, 16, 32))
    out = np.zeros((n_anchors, 85), dtype=np.float32)
    # stride 8 のグリッド (gx=10, gy=5) にボックスを置く: 中心 (10+0.5)*8=84, (5+0.5)*8=44、幅 exp(ln 4)*8=32
    idx = 5 * (416 // 8) + 10
    out[idx, :4] = [0.5, 0.5, np.log(4), np.log(2)]
    out[idx, 4] = 0.9  # objectness
    out[idx, 5 + 0] = 0.8  # person
    frame = Frame(tensor=None, ratio=0.5, width=832, height=468)
    dets = postprocess(out, frame, hw, score_threshold=0.3)
    assert len(dets) == 1
    d = dets[0]
    assert d.label == "person"
    assert d.confidence == pytest.approx(0.72)
    # 入力座標 (68, 36)-(100, 52) → 元画像 (136, 72)-(200, 104) → 正規化
    assert d.x == pytest.approx(136 / 832)
    assert d.y == pytest.approx(72 / 468)
    assert d.w == pytest.approx(64 / 832)
    assert d.h == pytest.approx(32 / 468)


def _models():
    names = ["yolox_nano.dyn", "yolox_tiny.dyn", "yolox_tiny.int8", "yolox_nano.int8"]
    return [n for n in names if os.path.exists(os.path.join(MODELS, n + ".onnx"))]


@pytest.mark.parametrize("model", _models() or [pytest.param("none", marks=pytest.mark.skip("モデルがない"))])
@pytest.mark.parametrize("width", [416, 320])
def test_person_detected(model, width):
    with open(os.path.join(FIXTURES, "person.jpg"), "rb") as f:
        data = f.read()
    det = Detector(os.path.join(MODELS, model + ".onnx"), input_width=width)
    [dets] = det.detect([data])
    persons = [d for d in dets if d.label == "person"]
    assert persons, dets
    p = max(persons, key=lambda d: d.confidence)
    assert p.confidence > 0.5
    # person.jpg(640x424)の人物はおおよそ x=190〜275, y=95〜380 にいる
    cx, cy = p.x + p.w / 2, p.y + p.h / 2
    assert 0.25 < cx < 0.5 and 0.3 < cy < 0.7
    assert 0 <= p.x and p.x + p.w <= 1 and 0 <= p.y and p.y + p.h <= 1
    # 犬と馬も写っている
    labels = {d.label for d in dets}
    assert "horse" in labels or "dog" in labels


def test_batch_matches_single():
    path = os.path.join(MODELS, "yolox_nano.dyn.onnx")
    if not os.path.exists(path):
        pytest.skip("モデルがない")
    with open(os.path.join(FIXTURES, "person.jpg"), "rb") as f:
        data = f.read()
    single = Detector(path, batch_size=1).detect([data] * 3)
    batched = Detector(path, batch_size=3).detect([data] * 3)
    for a, b in zip(single, batched):
        assert [d.label for d in a] == [d.label for d in b]
        for da, db in zip(a, b):
            assert da.x == pytest.approx(db.x, abs=1e-4)
