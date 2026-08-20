"""Top-level convenience entrypoint tying the pieces in PLAN.md §6 together."""

from __future__ import annotations

from typing import Any

from .graph import PipelineDeps, PipelineState, build_graph, default_deps
from .models import ResearchAnswer, RetrievalBackend

__all__ = ["run_research"]


async def run_research(
    question: str,
    *,
    model: Any = None,
    retrieval_backend: RetrievalBackend = "antigravity",
    use_headless_browser: bool = False,
    deps: PipelineDeps | None = None,
) -> ResearchAnswer:
    """Runs the full research pipeline (PLAN.md §6) end-to-end for `question`.

    Args:
        question: The user's research question.
        model: The `pydantic_ai` model used for both the Planner and Writer
            agents (e.g. `"google-gla:gemini-2.5-flash"`, or a `TestModel`
            for deterministic tests). Ignored if `deps` is supplied.
        retrieval_backend: Which `RetrievalGateway` backend to use — the
            default Antigravity SDK gateway, or the `pydantic_native`
            `WebSearch`/`WebFetch` gateway (PLAN.md §1).
        use_headless_browser: Wrap the selected backend with
            `BrowserAugmentedGateway` (see browser_gateway.py) so `.read()`
            renders JavaScript-heavy pages with real Chromium instead of a
            static HTML fetch. Requires the `browser` extra. Ignored if
            `deps` is supplied.
        deps: Pre-built `PipelineDeps` (agents + gateway factory), for tests
            that need full control over every collaborator. When omitted,
            `default_deps(model, use_headless_browser=use_headless_browser)`
            builds real Planner/Writer agents.

    Returns:
        The validated, grounded `ResearchAnswer`.
    """
    graph = build_graph()
    state = PipelineState(question=question, retrieval_backend=retrieval_backend)
    resolved_deps = (
        deps if deps is not None else default_deps(model, use_headless_browser=use_headless_browser)
    )
    return await graph.run(state=state, deps=resolved_deps)
