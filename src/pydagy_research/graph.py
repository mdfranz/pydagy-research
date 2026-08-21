"""Host Pipeline Workflow (PLAN.md §6): the `pydantic_graph` state machine.

    Planner -> Retrieval -> Validator -> Writer -> [CheckCitations] -> End
                                            ^______________|  (bounded retry)

Pydantic AI owns all reasoning (Planner, Writer); this module owns typed
state transitions, evidence pooling, and the citation-grounding gate that
makes the "deterministic evidence grounding" claim in PLAN.md's Summary hold.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_graph import BaseNode, End, Graph, GraphBuilder, GraphRunContext, StepContext

from .agents import WriterDeps, build_planner_agent, build_writer_agent, planner_prompt, writer_prompt
from .gateway import RetrievalGateway, make_gateway, run_plan
from .models import EvidenceRecord, ResearchAnswer, ResearchPlan, RetrievalBackend
from .telemetry import TelemetryRecorder

__all__ = [
    "PipelineState",
    "PipelineDeps",
    "PlannerNode",
    "RetrievalNode",
    "ValidatorNode",
    "WriterNode",
    "build_graph",
]

_logger = logging.getLogger(__name__)


@dataclass
class PipelineState:
    """Mutable state threaded through the graph run (PLAN.md §6).

    Carries both the research evidence and the operational telemetry from
    retrieval attempts (MULTI-PROVIDER-PLAN.md §5).
    """

    question: str
    retrieval_backend: RetrievalBackend = "antigravity"
    plan: ResearchPlan | None = None
    raw_evidence: list[EvidenceRecord] = field(default_factory=list)
    evidence_pool: dict[str, EvidenceRecord] = field(default_factory=dict)
    write_attempts: int = 0
    validation_errors: list[str] = field(default_factory=list)
    source_attempts: list = field(default_factory=list)
    validation_summary: dict | None = None


@dataclass
class PipelineDeps:
    """Injected collaborators: the two Pydantic AI agents and the gateway factory.

    `gateway_factory` defaults to `make_gateway` (PLAN.md §1's
    `retrieval_backend`-keyed selection) but is overridable so tests can
    inject a fake `RetrievalGateway` without touching either SDK.

    `enable_telemetry_emission` controls whether TelemetryRecorder emits
    spans to Logfire (MULTI-PROVIDER-PLAN.md §5.1). Pass the return value
    of `configure_tracing()` to enable when LOGFIRE_TOKEN is set.
    """

    planner_agent: Any
    writer_agent: Any
    gateway_factory: Callable[[ResearchPlan], RetrievalGateway] = make_gateway
    max_write_attempts: int = 2
    enable_telemetry_emission: bool = False


@dataclass
class PlannerNode(BaseNode[PipelineState, PipelineDeps]):
    """1. Planner Node (PLAN.md §6.1): generates the bounded `ResearchPlan`."""

    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> "RetrievalNode":
        result = await ctx.deps.planner_agent.run(planner_prompt(ctx.state.question))
        plan: ResearchPlan = result.output
        # The host, not the model, controls which retrieval backend runs
        # (PLAN.md §1: "a config toggle, not a one-way architectural door").
        ctx.state.plan = plan.model_copy(update={"retrieval_backend": ctx.state.retrieval_backend})
        return RetrievalNode()


@dataclass
class RetrievalNode(BaseNode[PipelineState, PipelineDeps]):
    """2. Retrieval Gateway Node (PLAN.md §6.2): executes the plan sequentially.

    Wires TelemetryRecorder into the gateway so all retrieval attempts are
    captured and emitted to Logfire (MULTI-PROVIDER-PLAN.md §5.1).
    """

    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> "ValidatorNode":
        assert ctx.state.plan is not None, "PlannerNode must run before RetrievalNode"

        # Create telemetry recorder for this retrieval run
        # Enable Logfire emission if tracing is configured (LOGFIRE_TOKEN set)
        from .telemetry import TelemetryEmitter

        emitter = TelemetryEmitter(enabled=ctx.deps.enable_telemetry_emission)
        recorder = TelemetryRecorder(emitter=emitter)

        # Pass recorder to gateway factory so it can emit attempt spans
        gateway = ctx.deps.gateway_factory(ctx.state.plan, telemetry_recorder=recorder)
        async with gateway:
            ctx.state.raw_evidence = await run_plan(gateway, ctx.state.plan)

        # Carry attempts through pipeline state for ResearchReport
        ctx.state.source_attempts = recorder.attempts
        return ValidatorNode()


@dataclass
class ValidatorNode(BaseNode[PipelineState, PipelineDeps]):
    """3. Evidence Validator Node (PLAN.md §6.3).

    Normalizes URLs, prunes near-duplicates (deduplicating by (url, provider)
    to allow multiple independent provider reads of the same URL for
    corroboration — MULTI-PROVIDER-PLAN.md §4), assigns stable `EVID-xxx`
    ids, and filters out failed fetches and drift-flagged records — the
    Writer Node only ever sees evidence that survived this gate.
    """

    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> "WriterNode":
        pool: dict[str, EvidenceRecord] = {}
        seen_keys: set[tuple[str, str | None]] = set()
        dropped_failed = 0
        dropped_drift = 0
        dropped_duplicate = 0
        next_index = 1

        for record in ctx.state.raw_evidence:
            if record.status != "success":
                dropped_failed += 1
                continue
            if record.drift_flagged:
                dropped_drift += 1
                continue

            normalized_url = record.source_url.strip().lower()
            # Dedup key is (url, provider): same URL from different providers is
            # a feature (corroboration), not a duplicate. Same URL from same
            # provider is a duplicate (skip).
            dedup_key = (normalized_url, record.provider)
            if dedup_key in seen_keys:
                dropped_duplicate += 1
                continue
            seen_keys.add(dedup_key)

            stable_id = f"EVID-{next_index:03d}"
            next_index += 1
            pool[stable_id] = record.model_copy(update={"evidence_id": stable_id})

        ctx.state.evidence_pool = pool
        ctx.state.validation_summary = {
            "raw_count": len(ctx.state.raw_evidence),
            "kept_count": len(pool),
            "dropped_failed": dropped_failed,
            "dropped_drift": dropped_drift,
            "dropped_duplicate": dropped_duplicate,
        }

        # Emit validation summary to telemetry if available
        # (TelemetryRecorder is passed to gateways, so we check if we have access via the evidence pool)
        _logger.info(
            "Evidence validation: raw=%d, kept=%d, dropped_failed=%d, dropped_drift=%d, dropped_duplicate=%d",
            len(ctx.state.raw_evidence),
            len(pool),
            dropped_failed,
            dropped_drift,
            dropped_duplicate,
        )

        return WriterNode()


@dataclass
class WriterNode(BaseNode[PipelineState, PipelineDeps, ResearchAnswer]):
    """4. Grounded Writer Node (PLAN.md §6.4) + the `CheckCitations` decision gate.

    The Writer agent's own `output_validator` (see `agents.build_writer_agent`)
    already retries in-place against `ModelRetry`. This node is the outer,
    bounded backstop: if those in-agent retries are exhausted and validation
    still fails, it loops back to itself (mirroring the diagram's
    `CheckCitations -- No --> WriteNode` edge) up to `max_write_attempts`
    times before degrading to a limitations-only answer rather than emitting
    an ungrounded one.
    """

    async def run(
        self, ctx: GraphRunContext[PipelineState, PipelineDeps]
    ) -> "WriterNode | End[ResearchAnswer]":
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        ctx.state.write_attempts += 1
        prompt = writer_prompt(ctx.state.question, ctx.state.evidence_pool)
        deps = WriterDeps(evidence_pool=ctx.state.evidence_pool)
        try:
            result = await ctx.deps.writer_agent.run(prompt, deps=deps)
        except UnexpectedModelBehavior as exc:
            _logger.warning(
                "Writer Node failed grounding validation (attempt %d/%d): %s",
                ctx.state.write_attempts,
                ctx.deps.max_write_attempts,
                exc,
            )
            ctx.state.validation_errors.append(str(exc))
            if ctx.state.write_attempts < ctx.deps.max_write_attempts:
                return WriterNode()
            return End(
                ResearchAnswer(
                    answer="",
                    claims=[],
                    citations=[],
                    limitations=[
                        "Unable to produce a fully grounded answer from the retrieved evidence"
                        f" after {ctx.state.write_attempts} attempt(s): {ctx.state.validation_errors[-1]}"
                    ],
                )
            )
        return End(result.output)


def build_graph() -> Graph[PipelineState, PipelineDeps, None, ResearchAnswer]:
    """Assembles the `pydantic_graph` Graph described in PLAN.md §6."""
    g = GraphBuilder(
        name="research_pipeline",
        state_type=PipelineState,
        deps_type=PipelineDeps,
        output_type=ResearchAnswer,
    )

    @g.step
    async def start(ctx: StepContext[PipelineState, PipelineDeps, None]) -> PlannerNode:
        return PlannerNode()

    g.add(
        g.node(PlannerNode),
        g.node(RetrievalNode),
        g.node(ValidatorNode),
        g.node(WriterNode),
        g.edge_from(g.start_node).to(start),
    )
    return g.build()


def default_deps(
    model: Any,
    *,
    use_headless_browser: bool = False,
    enable_otel_tracing: bool = False,
    multi_provider: list[str] | None = None,
    model_map: dict[str, str] | None = None,
) -> PipelineDeps:
    """Convenience: build `PipelineDeps` with real Planner/Writer agents on `model`.

    `use_headless_browser=True` wraps whichever backend `retrieval_backend`
    selects with `BrowserAugmentedGateway` (requires the `browser` extra —
    see browser_gateway.py), so `.read()` renders JavaScript-heavy pages
    with real Chromium instead of a static HTML fetch.

    `enable_otel_tracing=True` additionally instruments the Antigravity
    backend's own session/tool-call lifecycle with the SDK's built-in OTel
    hooks (irrelevant for `pydantic_native`, which `logfire.instrument_
    pydantic_ai()` already covers — see tracing.py). Typically passed
    through from `tracing.configure_tracing()`'s return value rather than
    set independently, so tracing the Antigravity subprocess just follows
    from `LOGFIRE_TOKEN` being set.
    """

    def gateway_factory(plan: ResearchPlan, telemetry_recorder=None, **kwargs) -> RetrievalGateway:
        # Multi-provider mode: use MultiProviderGateway instead of single backend
        if multi_provider and model_map:
            from .multi_provider_factory import make_multi_provider_gateway

            gateway = make_multi_provider_gateway(
                plan, providers=multi_provider, model_map=model_map, telemetry_recorder=telemetry_recorder
            )
        else:
            # Single-provider mode (original behavior)
            # PydanticNativeSearchGateway wraps a pydantic_ai.Agent and, unlike
            # AntigravitySDKGateway (which resolves its own model via
            # LocalAgentConfig/ADC/env when none is given), has no default model
            # to fall back to -- it needs the same `model` the Planner/Writer
            # agents use. AntigravitySDKGateway's `model` kwarg is a *different*
            # namespace (the Antigravity SDK's own model identifiers, not
            # pydantic_ai's provider-prefixed ones), so it must NOT get `model`
            # here -- pass nothing and let it resolve its own default.
            if plan.retrieval_backend == "pydantic_native":
                gateway = make_gateway(plan, model=model, telemetry_recorder=telemetry_recorder)
            else:
                gateway = make_gateway(
                    plan, enable_otel=enable_otel_tracing, telemetry_recorder=telemetry_recorder
                )

        if use_headless_browser:
            from .browser_gateway import BrowserAugmentedGateway

            gateway = BrowserAugmentedGateway(gateway, telemetry_recorder=telemetry_recorder)
        return gateway

    return PipelineDeps(
        planner_agent=build_planner_agent(model),
        writer_agent=build_writer_agent(model),
        gateway_factory=gateway_factory,
        enable_telemetry_emission=enable_otel_tracing,
    )
