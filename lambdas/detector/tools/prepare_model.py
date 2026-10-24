"""公式の YOLOX ONNX(1x3x416x416 固定)を Lambda 用に加工する。

- dyn:  入力の N/H/W を可変にする(Reshape の batch=1 固定を 0 に直す)。
        16:9 の画像を 416x256 のように縦を詰めて推論でき、バッチ化もできる。
- int8: dyn を静的量子化(QDQ、重みは per-channel の int8、活性は uint8)。
        キャリブレーションには calib_dir の画像を使う。

使い方:
  uv run python tools/prepare_model.py models/yolox_nano.onnx --calib-dir models/calib
  → models/yolox_nano.dyn.onnx、models/yolox_nano.int8.onnx
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import onnx
from onnx import numpy_helper

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from detect import decode_jpeg, input_size_for, preprocess  # noqa: E402


def make_dynamic(src: str, dst: str) -> None:
    m = onnx.load(src)
    inp = m.graph.input[0].type.tensor_type.shape.dim
    for d, name in zip(inp, ("N", "C", "H", "W")):
        if name != "C":
            d.ClearField("dim_value")
            d.dim_param = name
    out = m.graph.output[0].type.tensor_type.shape.dim
    out[0].ClearField("dim_value")
    out[0].dim_param = "N"
    out[1].ClearField("dim_value")
    out[1].dim_param = "A"
    inits = {i.name: i for i in m.graph.initializer}
    for n in m.graph.node:
        if n.op_type == "Reshape" and n.input[1] in inits:
            arr = numpy_helper.to_array(inits[n.input[1]]).copy()
            if arr.size == 3 and arr[0] == 1:
                arr[0] = 0  # 0 = 入力の次元をそのまま使う
                inits[n.input[1]].CopyFrom(numpy_helper.from_array(arr, n.input[1]))
    # 中間テンソルの固定 shape 情報を消して、推論し直させる
    del m.graph.value_info[:]
    # per-channel 量子化(DequantizeLinear の axis)には opset 13 以上が要る
    from onnx import version_converter

    m = version_converter.convert_version(m, 13)
    m = onnx.shape_inference.infer_shapes(m)
    onnx.checker.check_model(m)
    onnx.save(m, dst)


def calib_tensors(calib_dir: str, width: int) -> list[np.ndarray]:
    """キャリブレーション用の入力。各画像を 16:9 と元のアスペクト比で切り出して使う。"""
    from PIL import Image

    out = []
    for p in sorted(glob.glob(os.path.join(calib_dir, "*.jpg"))):
        with open(p, "rb") as f:
            img = decode_jpeg(f.read())
        w, h = img.size
        variants = [img]
        ch = int(w * 9 / 16)
        if ch <= h:
            top = (h - ch) // 2
            variants.append(img.crop((0, top, w, top + ch)))
        variants.append(img.transpose(Image.FLIP_LEFT_RIGHT))
        # ヒストグラム系のキャリブレーションは入力 shape をそろえる必要があるので、16:9 の枠に収める
        for v in variants:
            out.append(preprocess(v, input_size_for(16, 9, width)).tensor[None])
    return out


def quantize_int8(src: str, dst: str, calib_dir: str, width: int) -> None:
    from onnxruntime.quantization import (
        CalibrationDataReader,
        CalibrationMethod,
        QuantFormat,
        QuantType,
        quantize_static,
    )
    from onnxruntime.quantization.shape_inference import quant_pre_process

    tensors = calib_tensors(calib_dir, width)
    print(f"キャリブレーション入力: {len(tensors)} 件")

    class Reader(CalibrationDataReader):
        def __init__(self) -> None:
            self.it = iter(tensors)

        def get_next(self):
            t = next(self.it, None)
            return None if t is None else {"images": t}

    pre = dst + ".pre.onnx"
    quant_pre_process(src, pre, skip_symbolic_shape=True)
    quantize_static(
        pre,
        dst,
        Reader(),
        quant_format=QuantFormat.QDQ,
        per_channel=True,
        activation_type=QuantType.QUInt8,
        weight_type=QuantType.QInt8,
        calibrate_method=CalibrationMethod.Percentile,
        extra_options={"CalibPercentile": 99.99},
    )
    os.remove(pre)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--calib-dir")
    ap.add_argument("--width", type=int, default=416)
    a = ap.parse_args()
    base = a.model.removesuffix(".onnx")
    make_dynamic(a.model, base + ".dyn.onnx")
    print("作成:", base + ".dyn.onnx")
    if a.calib_dir:
        quantize_int8(base + ".dyn.onnx", base + ".int8.onnx", a.calib_dir, a.width)
        print("作成:", base + ".int8.onnx")


if __name__ == "__main__":
    main()
