"""YOLOX(ONNX Runtime、CPU)による物体検出。前処理・推論・後処理(デコード、NMS)。

YOLOX の前処理は「BGR、0〜255 のまま(正規化なし)、右下を 114 で埋めるレターボックス」。
モデルは tools/prepare_model.py で入力の N/H/W を可変にしたもの(*.dyn.onnx)を想定するが、
公式の固定サイズ(1x3x416x416)のモデルもそのまま使える。
"""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)

STRIDES = (8, 16, 32)
PAD_VALUE = 114


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    # 0〜1 に正規化した左上原点のボックス
    x: float
    y: float
    w: float
    h: float


@dataclass
class Frame:
    """前処理済みの 1 フレーム。"""

    tensor: np.ndarray  # (3, H, W) float32, BGR
    ratio: float  # 元画像 → 入力テンソルの縮小率
    width: int  # 元画像の幅
    height: int  # 元画像の高さ


def input_size_for(width: int, height: int, target_width: int) -> tuple[int, int]:
    """元画像のアスペクト比を保ったまま幅を target_width にしたときの入力サイズ (H, W)。

    H は 32 の倍数に切り上げる(YOLOX の最大ストライド)。16:9、幅 416 なら (256, 416)。
    """
    h = round(height * target_width / width)
    return (-(-h // 32) * 32, target_width)


def decode_jpeg(data: bytes, target_width: int | None = None) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    if target_width and img.format == "JPEG" and img.width > target_width:
        # JPEG はデコード時に 1/2〜1/8 に縮小できる(draft)。大きな画像が来ても速い
        img.draft("RGB", (target_width, round(img.height * target_width / img.width)))
    return img.convert("RGB")


def preprocess(img: Image.Image, input_hw: tuple[int, int]) -> Frame:
    """レターボックス(左上寄せ、残りを 114 で埋める)して CHW の BGR float32 にする。"""
    ih, iw = input_hw
    w, h = img.size
    r = min(ih / h, iw / w)
    nw, nh = max(1, int(w * r)), max(1, int(h * r))
    if (nw, nh) != (w, h):
        img = img.resize((nw, nh), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.uint8)
    canvas = np.full((ih, iw, 3), PAD_VALUE, dtype=np.uint8)
    canvas[:nh, :nw] = arr
    # RGB → BGR、HWC → CHW
    tensor = np.ascontiguousarray(canvas[:, :, ::-1].transpose(2, 0, 1), dtype=np.float32)
    return Frame(tensor=tensor, ratio=r, width=w, height=h)


def _grids(input_hw: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    ih, iw = input_hw
    grids, strides = [], []
    for s in STRIDES:
        gh, gw = ih // s, iw // s
        yv, xv = np.meshgrid(np.arange(gh), np.arange(gw), indexing="ij")
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        strides.append(np.full((gh * gw, 1), s))
    return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """xyxy のボックスに対する素朴な NMS。残すインデックスを返す。"""
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_threshold]
    return keep


def postprocess(
    output: np.ndarray,
    frame: Frame,
    input_hw: tuple[int, int],
    score_threshold: float = 0.3,
    nms_threshold: float = 0.45,
    max_detections: int = 100,
) -> list[Detection]:
    """1 フレームぶんの生出力 (A, 85) をデコードし、クラスごとの NMS をかけて正規化座標で返す。"""
    grids, strides = _grids(input_hw)
    xy = (output[:, :2] + grids) * strides
    wh = np.exp(output[:, 2:4]) * strides
    cls_scores = output[:, 5:] * output[:, 4:5]
    cls_ids = cls_scores.argmax(1)
    scores = cls_scores[np.arange(len(cls_ids)), cls_ids]
    mask = scores >= score_threshold
    if not mask.any():
        return []
    xy, wh, scores, cls_ids = xy[mask], wh[mask], scores[mask], cls_ids[mask]
    # 入力テンソル座標 → 元画像座標
    boxes = np.concatenate([xy - wh / 2, xy + wh / 2], 1) / frame.ratio
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, frame.width)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, frame.height)
    # クラスごとの NMS(クラス ID ぶんずらして 1 回で済ませる)
    offset = cls_ids[:, None].astype(np.float32) * (max(frame.width, frame.height) + 1)
    keep = nms(boxes + offset, scores, nms_threshold)[:max_detections]
    dets = []
    for i in keep:
        x1, y1, x2, y2 = boxes[i]
        if x2 - x1 < 1 or y2 - y1 < 1:
            continue
        dets.append(
            Detection(
                label=COCO_CLASSES[int(cls_ids[i])],
                confidence=float(scores[i]),
                x=float(x1 / frame.width),
                y=float(y1 / frame.height),
                w=float((x2 - x1) / frame.width),
                h=float((y2 - y1) / frame.height),
            )
        )
    return dets


class Detector:
    """ONNX Runtime のセッションを持ち、JPEG のリストをまとめて推論する。"""

    def __init__(
        self,
        model_path: str,
        *,
        input_width: int = 416,
        threads: int = 0,
        batch_size: int = 1,
        graph_opt: str = "all",
        score_threshold: float = 0.3,
        nms_threshold: float = 0.45,
    ) -> None:
        import onnxruntime as ort

        t0 = time.perf_counter()
        so = ort.SessionOptions()
        so.graph_optimization_level = {
            "disable": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
            "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
            "extended": ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
            "all": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
        }[graph_opt]
        if threads > 0:
            so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])
        self.load_ms = (time.perf_counter() - t0) * 1000
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        shape = inp.shape
        # 固定サイズのモデルなら、その H/W と batch=1 に合わせる
        self.fixed_hw = (shape[2], shape[3]) if all(isinstance(d, int) for d in shape[2:]) else None
        self.batch_size = 1 if isinstance(shape[0], int) else max(1, batch_size)
        self.input_width = input_width
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.model_name = os.path.basename(model_path).split(".")[0]

    def input_hw(self, width: int, height: int) -> tuple[int, int]:
        return self.fixed_hw or input_size_for(width, height, self.input_width)

    def detect(self, images: list[bytes], timings: dict | None = None) -> list[list[Detection]]:
        """JPEG のリストを推論し、フレームごとの検出結果を返す。timings に各段の ms を足す。"""
        timings = timings if timings is not None else {}
        t = time.perf_counter()
        frames: list[Frame] = []
        hws: list[tuple[int, int]] = []
        for data in images:
            img = decode_jpeg(data, self.fixed_hw[1] if self.fixed_hw else self.input_width)
            hw = self.input_hw(*img.size)
            frames.append(preprocess(img, hw))
            hws.append(hw)
        timings["preprocess_ms"] = timings.get("preprocess_ms", 0) + (time.perf_counter() - t) * 1000

        results: list[list[Detection]] = []
        i = 0
        while i < len(frames):
            # 同じ入力サイズのフレームだけを 1 バッチにまとめる
            j = i + 1
            while j < len(frames) and j - i < self.batch_size and hws[j] == hws[i]:
                j += 1
            batch = np.stack([f.tensor for f in frames[i:j]])
            t = time.perf_counter()
            out = self.session.run(None, {self.input_name: batch})[0]
            timings["inference_ms"] = timings.get("inference_ms", 0) + (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            for k, f in enumerate(frames[i:j]):
                results.append(
                    postprocess(out[k], f, hws[i], self.score_threshold, self.nms_threshold)
                )
            timings["postprocess_ms"] = timings.get("postprocess_ms", 0) + (time.perf_counter() - t) * 1000
            i = j
        return results
