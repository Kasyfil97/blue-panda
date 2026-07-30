"""Logging setup for auditing the generation flow.

Every module logs under the ``mage_flow`` logger namespace, so configuring that
one logger captures the whole flow (system context -> BM25 -> KATA -> Confluence
-> hypothesis -> LLM). Call ``configure_logging()`` from a CLI to print the trace.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """Route all mage_flow trace logs to stderr with a compact format."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))

    logger = logging.getLogger("mage_flow")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False


__all__ = ["configure_logging"]
