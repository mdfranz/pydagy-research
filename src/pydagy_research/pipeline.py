"""Top-level convenience entrypoint tying the pieces in PLAN.md §6 together."""

from __future__ import annotations

from typing import Any

from .graph import PipelineDeps, PipelineState, build_graph, default_deps
from .models import ResearchAnswer, ResearchReport, RetrievalBackend
from .models import ResearchPlan
from .telemetry import ExperimentContext

__all__ = ["run_research"]


async def run_research(
    question: str,
    *,
    model: Any = None,
    retrieval_backend: RetrievalBackend = "antigravity",
    use_headless_browser: bool = False,
    enable_otel_tracing: bool = False,
    multi_provider: list[str] | None = None,
    model_map: dict[str, str] | None = None,
    fixed_plan: ResearchPlan | None = None,
    telemetry_experiment: ExperimentContext | None = None,
    deps: PipelineDeps | None = None,
) -> ResearchReport:
    """Runs the full research pipeline (PLAN.md §6) end-to-end for `question`.

    Args:
        question: The user's research question.
        model: The `pydantic_ai` model used for Planner/Writer agents when not
            in multi-provider mode. Ignored if `deps` or `multi_provider` supplied.
        retrieval_backend: Which single-provider backend to use (PLAN.md §1).
            Ignored if `multi_provider` supplied. Default: "antigravity".
        use_headless_browser: Wrap backend with `BrowserAugmentedGateway` for
            JavaScript rendering. Requires `browser` extra. Ignored if `deps` supplied.
        enable_otel_tracing: Enable Logfire/OTel tracing (see tracing.py).
            Pass `configure_tracing()`'s return value. Ignored if `deps` supplied.
        multi_provider: List of provider names to run in parallel
            (e.g., ["gemini", "anthropic"]). When set, uses
            `MultiProviderGateway` instead of single backend. Requires `model_map`.
        model_map: {provider_name: model_id, ...} mapping when using `multi_provider`.
            e.g., {"gemini": "google:gemini-3.7-flash", "anthropic": "anthropic:claude-opus-5"}
        deps: Pre-built `PipelineDeps` for tests needing full control.
            When omitted, `default_deps()` builds real agents and gateway.

    Returns:
        A `ResearchReport` containing the validated, grounded answer plus
        operational telemetry (MULTI-PROVIDER-PLAN.md §5): source attempt
        records and validation summary showing what evidence was kept/dropped.
    """
    graph = build_graph()
    state = PipelineState(question=question, retrieval_backend=retrieval_backend)
    resolved_deps = (
        deps
        if deps is not None
        else default_deps(
            model,
            use_headless_browser=use_headless_browser,
            enable_otel_tracing=enable_otel_tracing,
            multi_provider=multi_provider,
            model_map=model_map,
            fixed_plan=fixed_plan,
            telemetry_experiment=telemetry_experiment,
        )
    )
    answer = await graph.run(state=state, deps=resolved_deps)
    # Convert dropped records to serializable format
    dropped_records_data = [
        (record.model_dump(), reason) for record, reason in state.dropped_records
    ]
    return ResearchReport(
        answer=answer,
        source_attempts=state.source_attempts,
        validation_summary=state.validation_summary,
        dropped_records=dropped_records_data,
    )
