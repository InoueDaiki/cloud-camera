"""起動: python -m onvif_emulator [--config config.yaml]"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .app import run
from .config import load_config


def main() -> None:
    ap = argparse.ArgumentParser(description="ONVIF カメラエミュレーター")
    ap.add_argument("--config", help="YAML の設定ファイル(環境変数 EMULATOR_CONFIG でも可)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(
        level=cfg.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
