"""Shared command-line logging configuration."""

import logging
import os


def configure_logging() -> None:
    """Send pipeline progress to stderr so Docker and supercronic retain it."""
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Uvicorn installs its own handlers before the application lifespan starts.
    # Giving our namespace an explicit level ensures application INFO messages are
    # not discarded even when Uvicorn leaves the root logger at WARNING.
    logging.getLogger("api").setLevel(level)
