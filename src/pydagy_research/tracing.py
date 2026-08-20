"""Optional Logfire tracing, enabled only when `LOGFIRE_TOKEN` is set.

`pydantic_ai` has first-class Logfire integration: `logfire.configure()` +
`logfire.instrument_pydantic_ai()` traces every agent run, tool call, and
model request across the Planner/Writer nodes (agents.py) and the
`pydantic_native` gateway's internal search/fetch agents (gateway.py) — each
now has an explicit `name=` (`planner_agent`, `writer_agent`,
`native_search_agent`, `native_fetch_agent`) so their spans are legible in
the Logfire UI instead of all collapsing to a generic `agent` label.

Deliberately gated on `LOGFIRE_TOKEN` rather than calling
`logfire.configure()` unconditionally: without a token, `configure()` would
still run locally (`send_to_logfire` defaults to `'if-token-present'`), but
`instrument_pydantic_ai()` still adds span-wrapping overhead to every agent
call for traces that go nowhere — skip both entirely when there's nothing
to send, matching what was actually asked for ("if LOGFIRE_TOKEN is set").

Deliberately does NOT call `logfire.instrument_httpx(capture_all=True)`:
that captures exact provider payloads — prompts, tool arguments, tool
results, potentially secrets — and is meant for targeted, opt-in debugging,
not a default-on tracing path.

CLI-only, like `logging_config.configure_file_logging()` — a library
function shouldn't have the side effect of configuring a process-wide
tracer; only the `main()` entrypoint opts into that. Call this yourself
first if you're using `pydagy_research` as a library and want tracing.
"""

from __future__ import annotations

import logging
import os

__all__ = ["configure_tracing"]

_logger = logging.getLogger(__name__)


def configure_tracing() -> bool:
    """Configures Logfire tracing if `LOGFIRE_TOKEN` is set.

    Returns:
        `True` if tracing was configured, `False` if `LOGFIRE_TOKEN` wasn't
        set (a no-op in that case — nothing else in the package is affected).
    """
    if not os.environ.get("LOGFIRE_TOKEN"):
        return False

    import logfire

    logfire.configure()
    logfire.instrument_pydantic_ai()
    _logger.info("Logfire tracing enabled (LOGFIRE_TOKEN set)")
    return True
