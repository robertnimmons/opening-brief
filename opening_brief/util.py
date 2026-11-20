"""Small shared helpers: retry with backoff and logging setup."""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")
log = logging.getLogger("opening_brief")


def retry_once(fn: Callable[[], T], what: str, backoff: float = 2.0) -> T:
    """Call fn; on any exception wait `backoff` seconds and try exactly once more."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("%s failed (%s); retrying once in %.0fs", what, exc, backoff)
        time.sleep(backoff)
        return fn()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # yfinance is chatty at INFO
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
