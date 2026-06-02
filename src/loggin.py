"""logging interface for the Pocket Network agent."""

import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for the application."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    # Silence overly verbose third-party loggers
    for noisy in ("httpx", "httpcore", "openai", "langchain", "langgraph"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
