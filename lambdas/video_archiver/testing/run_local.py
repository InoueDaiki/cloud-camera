"""ハンドラーをローカルから実 AWS に対して 1 回実行する(検証用)。

    cd lambdas/video_archiver
    STATE_TABLE=... MEDIA_BUCKET=... STREAM_NAME_PREFIX=... \\
      mise exec -- uv run python testing/run_local.py cam-001
"""

import json
import sys
import time
import uuid
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]

import handler  # noqa: E402


class Ctx:
    def __init__(self, timeout_s=60):
        self.deadline = time.monotonic() + timeout_s
        self.aws_request_id = f"local-{uuid.uuid4()}"

    def get_remaining_time_in_millis(self):
        return int((self.deadline - time.monotonic()) * 1000)


if __name__ == "__main__":
    res = handler.lambda_handler({"camera_id": sys.argv[1]}, Ctx())
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
