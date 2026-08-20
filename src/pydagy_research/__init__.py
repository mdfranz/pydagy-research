"""Pydantic-first Antigravity research architecture (see PLAN.md)."""

from .browser_gateway import BrowserAugmentedGateway
from .gateway import AntigravitySDKGateway, PydanticNativeSearchGateway, RetrievalGateway, make_gateway
from .graph import PipelineDeps, PipelineState, build_graph, default_deps
from .models import (
    Citation,
    Claim,
    EvidenceRecord,
    ResearchAnswer,
    ResearchPlan,
    SearchOrFetchRequest,
    validate_research_answer,
)
from .pipeline import run_research

__all__ = [
    "BrowserAugmentedGateway",
    "AntigravitySDKGateway",
    "PydanticNativeSearchGateway",
    "RetrievalGateway",
    "make_gateway",
    "PipelineDeps",
    "PipelineState",
    "build_graph",
    "default_deps",
    "Citation",
    "Claim",
    "EvidenceRecord",
    "ResearchAnswer",
    "ResearchPlan",
    "SearchOrFetchRequest",
    "validate_research_answer",
    "run_research",
    "main",
]


def main() -> None:
    """CLI smoke entrypoint: runs one research question against a live model.

    Requires a configured `pydantic_ai` model provider (e.g. `GEMINI_API_KEY`
    for the default `retrieval_backend="antigravity"` — see PLAN.md
    "Runtime & Environment Requirements").

    Logs the full run (this package's + the Antigravity SDK's own
    session/tool-call trace) to `./pydagy-research.log`, appended across
    runs. Override with `PYDAGY_RESEARCH_LOG_FILE` / `PYDAGY_RESEARCH_LOG_LEVEL`.

    Pass `--browser` to render `.read()` pages with real headless Chromium
    instead of a static HTML fetch (requires `uv sync --extra browser` +
    `uv run playwright install chromium` — see browser_gateway.py).
    """
    import asyncio
    import sys

    from .logging_config import configure_file_logging

    log_path = configure_file_logging()
    print(f"(logging to {log_path})", file=sys.stderr)

    args = sys.argv[1:]
    use_headless_browser = "--browser" in args
    args = [a for a in args if a != "--browser"]

    question = " ".join(args) or "What is the latest stable Python release?"
    answer = asyncio.run(
        run_research(question, model="google:gemini-3.7-flash", use_headless_browser=use_headless_browser)
    )
    print(answer.model_dump_json(indent=2))
