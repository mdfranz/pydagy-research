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
    ResearchReport,
    SearchOrFetchRequest,
    validate_research_answer,
)
from .pipeline import run_research
from .telemetry import (
    ExperimentContext,
    RetrievalRequestTelemetry,
    SourceAttempt,
    TelemetryEmitter,
    TelemetryRecorder,
    ValidationSummary,
)

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
    "ResearchReport",
    "SearchOrFetchRequest",
    "validate_research_answer",
    "run_research",
    "ExperimentContext",
    "RetrievalRequestTelemetry",
    "SourceAttempt",
    "TelemetryEmitter",
    "TelemetryRecorder",
    "ValidationSummary",
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

    Also enables Logfire tracing of every agent run/tool call/model request
    when `LOGFIRE_TOKEN` is set in the environment — a no-op otherwise.
    """
    import argparse
    import asyncio
    import sys

    from .logging_config import configure_file_logging
    from .tracing import configure_tracing

    parser = argparse.ArgumentParser(
        prog="pydagy-research", description="Run one question through the grounded research pipeline (PLAN.md §6)."
    )
    parser.add_argument(
        "question", nargs="*", help="The research question (default: a Python-release smoke question)."
    )
    parser.add_argument(
        "--backend",
        choices=["antigravity", "pydantic_native"],
        default="antigravity",
        help="Retrieval backend (PLAN.md §1). Default: antigravity.",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "Render .read() pages with real headless Chromium instead of a static HTML"
            " fetch, on either backend (requires `uv sync --extra browser` + `uv run"
            " playwright install chromium` — see browser_gateway.py)."
        ),
    )
    parser.add_argument(
        "--model",
        default="google:gemini-3.7-flash",
        help="pydantic_ai model id for the Planner/Writer agents.",
    )
    args = parser.parse_args()

    log_path = configure_file_logging()
    print(f"(logging to {log_path})", file=sys.stderr)

    tracing_enabled = configure_tracing()
    if tracing_enabled:
        print("(logfire tracing enabled, including the Antigravity SDK's own spans)", file=sys.stderr)

    question = " ".join(args.question) or "What is the latest stable Python release?"
    report = asyncio.run(
        run_research(
            question,
            model=args.model,
            retrieval_backend=args.backend,
            use_headless_browser=args.browser,
            enable_otel_tracing=tracing_enabled,
        )
    )
    print(report.model_dump_json(indent=2))
