"""削った zip の中身で import と推論、Parquet の読み書きができるかを確かめる(build.sh の SMOKE=1)。

Lambda のベースイメージで /var/task に展開済みのステージを置いて実行する。
"""

import glob
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/var/task")
t = time.perf_counter()
import handler  # noqa: E402,F401  common、boto3 も含めて import できること
import parquet_store  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from detect import Detector  # noqa: E402

print(f"import: {(time.perf_counter() - t) * 1000:.0f} ms")

data = open("/fixtures/person.jpg", "rb").read()
for path in sorted(glob.glob("/var/task/models/*.onnx")):
    det = Detector(path)
    [dets] = det.detect([data])
    labels = sorted({d.label for d in dets})
    print(os.path.basename(path), f"load {det.load_ms:.0f} ms", labels)
    assert "person" in labels

start = datetime(2026, 10, 24, 10, 20, tzinfo=timezone.utc)
rows = [{"camera_id": "cam-001", "ts": start + timedelta(seconds=i), "label": "person",
         "confidence": 0.9, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "model": "m"} for i in range(5)]
t1 = parquet_store.rows_to_table(rows)
merged, removed = parquet_store._merge(t1, t1, start, start + timedelta(minutes=1))
assert removed == 5 and merged.num_rows == 5
back = pq.ParquetFile(io.BytesIO(parquet_store._serialize(merged))).read()
assert back.num_rows == 5
print("parquet: ok")

# PyAV(pyav_min、arm64 の zip だけ): /var/task/lib の FFmpeg を読み込めて、HEVC / H.264 のデコーダがあること
if os.path.isdir("/var/task/av"):
    import av  # noqa: E402

    names = {c for c in av.codecs_available}
    assert {"hevc", "h264"} <= names, names
    print(f"pyav: {av.__version__} ffmpeg {av.library_versions.get('libavcodec')} decoders ok")
print("SMOKE OK")
# Rosetta 上の x86_64 では ONNX Runtime のスレッドプールが終了時に止まることがあるので、明示的に抜ける
sys.stdout.flush()
os._exit(0)
