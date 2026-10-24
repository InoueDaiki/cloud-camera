"""構造化ログ(1 行 1 JSON)。CloudWatch Logs Insights でそのまま集計できる形にする。"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (bytes, bytearray)):
        return f"<{len(o)} bytes>"
    return str(o)


def log(event: str, **fields: Any) -> None:
    rec = {"event": event, **fields}
    print(json.dumps(rec, ensure_ascii=False, default=_default), file=sys.stdout, flush=True)


def max_rss_mb() -> float:
    """このプロセスの最大常駐メモリ(MB)。Lambda の Max Memory Used の目安。"""
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux は KB、macOS はバイト
    if sys.platform == "darwin":
        return round(rss / 1024 / 1024, 1)
    return round(rss / 1024, 1)
