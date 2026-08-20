"""Pydantic-first Antigravity research architecture (see PLAN.md)."""

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
    """
    import asyncio
    import sys

    question = " ".join(sys.argv[1:]) or "What is the latest stable Python release?"
    answer = asyncio.run(run_research(question, model="google:gemini-3.7-flash"))
    print(answer.model_dump_json(indent=2))
