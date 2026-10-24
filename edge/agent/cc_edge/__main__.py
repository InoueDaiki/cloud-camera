"""python3 -m cc_edge でエージェントを起動する。"""

import asyncio
import logging
import sys

from . import config
from .agent import Agent


def main() -> int:
    cfg = config.load()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(Agent(cfg).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
