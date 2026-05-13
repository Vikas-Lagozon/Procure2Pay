# logger.py
# Global logging setup — one shared rotating daily log file.

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

_LOGS_DIR   = Path("logs")
_ROOT_NAME  = "Procure2Pay"


# ──────────────────────────────────────────────────────────────
# Bootstrap — executed once at import time
# ──────────────────────────────────────────────────────────────

def _bootstrap() -> logging.Logger:
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = _LOGS_DIR / f"log_{timestamp}.log"

    root = logging.getLogger(_ROOT_NAME)
    if root.handlers:
        return root

    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — DEBUG and above
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console handler — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(ch)
    root.info(f"[Logger] Bootstrapped. Log file: {log_file}")
    return root


_root_logger = _bootstrap()


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────

def get_logger(name: str = _ROOT_NAME) -> logging.Logger:
    """
    Return a named child logger that inherits the shared handlers.
    """
    if name == _ROOT_NAME:
        return _root_logger
    child = logging.getLogger(f"{_ROOT_NAME}.{name}")
    child.setLevel(logging.DEBUG)
    return child


# Convenience export — `from logger import logger`
logger = get_logger()

