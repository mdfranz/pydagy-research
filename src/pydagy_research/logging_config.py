"""File-based logging setup for local runs (CLI-only; not part of PLAN.md's
core pipeline design — added for debugging/observability of live runs).

`google.antigravity` and `pydantic_ai` both log through the *root* logger
(e.g. `agent.py` calls bare `logging.info("Starting Agent session")`, not
`logging.getLogger(__name__)`), so capturing their session/tool-call trace
requires attaching a handler to the root logger rather than a
package-scoped one. This is deliberately CLI-only, not wired into
`run_research()`/`pipeline.py`: a library function shouldn't have the
side effect of writing files in the caller's process — only the `main()`
CLI entrypoint opts into that.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

__all__ = ["configure_file_logging"]

DEFAULT_LOG_FILE = "pydagy-research.log"


def configure_file_logging(
    log_file: str | Path | None = None,
    *,
    level: int | None = None,
) -> Path:
    """Attaches a file handler to the root logger; returns the resolved path.

    Appends across runs (each run's entries are timestamped) rather than
    truncating, so a log file accumulates a history of local runs.

    Args:
        log_file: Where to write. Defaults to `$PYDAGY_RESEARCH_LOG_FILE` if
            set, else `./pydagy-research.log` (relative to the current
            working directory at call time, per the caller's request for a
            "local file in the current directory").
        level: Root logger level. Defaults to `$PYDAGY_RESEARCH_LOG_LEVEL`
            (a level name like "DEBUG"/"INFO"/"WARNING") if set, else INFO
            — verbose enough to capture the Antigravity SDK's own
            session/tool-call trace, which is usually the fastest way to
            see what the inner model actually did on a live run.
    """
    resolved_file = log_file or os.environ.get("PYDAGY_RESEARCH_LOG_FILE", DEFAULT_LOG_FILE)
    path = Path(resolved_file).resolve()

    if level is None:
        level_name = os.environ.get("PYDAGY_RESEARCH_LOG_LEVEL", "INFO").upper()
        level = logging.getLevelNamesMapping().get(level_name, logging.INFO)

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)

    return path
