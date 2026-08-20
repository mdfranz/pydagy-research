"""Graph-level tests (PLAN.md Test Plan: "Use TestModel / FunctionModel to
test Planner, Validator, and Writer nodes deterministically").

A fake `RetrievalGateway` stands in for both SDK backends so these tests
never touch `localharness` or a real model provider's network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic_ai.models.test import TestModel

from pydagy_research.agents import build_planner_agent, build_writer_agent
from pydagy_research.gateway import AntigravitySDKGateway, PydanticNativeSearchGateway, RetrievalGateway
from pydagy_research.graph import PipelineDeps, PipelineState, build_graph, default_deps
from pydagy_research.models import EvidenceRecord, ResearchPlan, SearchOrFetchRequest


class _FakeGateway:
    """A `RetrievalGateway` returning canned evidence, no SDK involved."""

    def __init__(self, search_records: list[EvidenceRecord], read_record: EvidenceRecord | None):
        self._search_records = search_records
        self._read_record = read_record

    async def __aenter__(self) -> "_FakeGateway":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
        return list(self._search_records)

    async def read(self, url: str) -> EvidenceRecord:
        assert self._read_record is not None
        return self._read_record


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fixed_plan(plan: ResearchPlan) -> ResearchPlan:
    """A planner_agent.run() stand-in producing a deterministic plan."""
    return plan


@pytest.fixture
def evidence():
    search = EvidenceRecord(
        evidence_id="RAW-search",
        source_url="search:capital of France",
        source_kind="search_summary",
        title="Search: capital of France",
        raw_extract="Paris is the capital of France, per several sources.",
        timestamp=_now(),
        status="success",
    )
    page = EvidenceRecord(
        evidence_id="RAW-page",
        source_url="https://example.com/france",
        source_kind="page_content",
        title="France - Wikipedia",
        raw_extract="Paris is the capital and most populous city of France.",
        timestamp=_now(),
        status="success",
    )
    return search, page


async def test_full_pipeline_success_path(evidence):
    search_record, page_record = evidence

    planner_plan = ResearchPlan(
        question="What is the capital of France?",
        requests=[
            SearchOrFetchRequest(request_id="r1", action="search", query_or_url="capital of France"),
            SearchOrFetchRequest(request_id="r2", action="read", query_or_url="https://example.com/france"),
        ],
    )
    planner_model = TestModel(custom_output_args=planner_plan.model_dump(mode="json"))

    def gateway_factory(plan: ResearchPlan) -> RetrievalGateway:
        return _FakeGateway(search_records=[search_record], read_record=page_record)

    # First pass: build the graph and peek at the evidence pool the
    # Validator Node produces, so the Writer's custom_output_args can cite
    # the *actual* stable EVID-xxx id rather than guessing it.
    graph = build_graph()
    state = PipelineState(question="What is the capital of France?", retrieval_backend="pydantic_native")
    probe_deps = PipelineDeps(
        planner_agent=build_planner_agent(planner_model, enable_search=False),
        writer_agent=build_writer_agent(TestModel(custom_output_args={
            "answer": "placeholder",
            "claims": [],
            "citations": [],
            "limitations": ["placeholder run to discover the evidence id"],
        })),
        gateway_factory=gateway_factory,
    )
    result = await graph.run(state=state, deps=probe_deps)
    assert result.limitations  # the placeholder writer output was accepted (no citations to validate)

    # The pool holds both the search_summary and the page_content record
    # (PLAN.md §6.3: the Validator Node doesn't drop search_summary evidence,
    # only failed/drift-flagged records — citability is enforced later, at
    # the Writer's grounding check). Only the page_content one is citable.
    [stable_id] = [
        eid for eid, rec in state.evidence_pool.items() if rec.source_kind == "page_content"
    ]

    # Second pass: same evidence, a writer that actually cites the real id.
    state2 = PipelineState(question="What is the capital of France?", retrieval_backend="pydantic_native")
    writer_output = {
        "answer": "Paris is the capital of France.",
        "claims": [{"claim_text": "Paris is the capital of France.", "evidence_ids": [stable_id]}],
        "citations": [{"evidence_id": stable_id, "source_url": page_record.source_url, "snippet": "Paris..."}],
        "limitations": [],
    }
    deps2 = PipelineDeps(
        planner_agent=build_planner_agent(planner_model, enable_search=False),
        writer_agent=build_writer_agent(TestModel(custom_output_args=writer_output)),
        gateway_factory=gateway_factory,
    )
    final = await graph.run(state=state2, deps=deps2)

    assert final.answer == "Paris is the capital of France."
    assert final.citations[0].evidence_id == stable_id
    assert not final.limitations


async def test_pipeline_degrades_gracefully_when_writer_cannot_ground(evidence):
    search_record, page_record = evidence
    planner_plan = ResearchPlan(
        question="q",
        requests=[SearchOrFetchRequest(request_id="r1", action="read", query_or_url="https://example.com/france")],
    )
    planner_model = TestModel(custom_output_args=planner_plan.model_dump(mode="json"))

    def gateway_factory(plan: ResearchPlan) -> RetrievalGateway:
        return _FakeGateway(search_records=[search_record], read_record=page_record)

    # Writer output cites an evidence_id that does not exist in the pool —
    # TestModel's default (unconstrained) generation will do this reliably,
    # driving the CheckCitations "No" path to exhaustion.
    graph = build_graph()
    state = PipelineState(question="q", retrieval_backend="pydantic_native")
    deps = PipelineDeps(
        planner_agent=build_planner_agent(planner_model, enable_search=False),
        writer_agent=build_writer_agent(TestModel()),
        gateway_factory=gateway_factory,
        max_write_attempts=2,
    )

    final = await graph.run(state=state, deps=deps)

    assert final.citations == []
    assert final.claims == []
    assert final.limitations
    assert state.write_attempts == 2


async def test_validator_node_deduplicates_and_drops_failed_and_drift_records():
    from pydagy_research.graph import ValidatorNode
    from pydantic_graph import GraphRunContext

    good = EvidenceRecord(
        evidence_id="RAW-1",
        source_url="https://example.com/a",
        source_kind="page_content",
        title="A",
        raw_extract="text a",
        timestamp=_now(),
        status="success",
    )
    duplicate = good.model_copy(update={"evidence_id": "RAW-1-dup", "source_url": "HTTPS://EXAMPLE.com/a "})
    failed = EvidenceRecord(
        evidence_id="RAW-2",
        source_url="https://example.com/b",
        source_kind="page_content",
        title="B",
        raw_extract="",
        timestamp=_now(),
        status="failed",
        error="boom",
    )
    drifted = EvidenceRecord(
        evidence_id="RAW-3",
        source_url="https://example.com/c",
        source_kind="page_content",
        title="C",
        raw_extract="text c",
        timestamp=_now(),
        status="success",
        drift_flagged=True,
    )

    state = PipelineState(question="q")
    state.raw_evidence = [good, duplicate, failed, drifted]
    deps = PipelineDeps(planner_agent=None, writer_agent=None)
    ctx = GraphRunContext(state=state, deps=deps)

    node = ValidatorNode()
    next_node = await node.run(ctx)

    assert next_node.__class__.__name__ == "WriterNode"
    assert list(state.evidence_pool.keys()) == ["EVID-001"]
    assert state.evidence_pool["EVID-001"].source_url == good.source_url


def test_default_deps_gateway_factory_threads_model_into_pydantic_native_only():
    """Regression test: PydanticNativeSearchGateway.model is a required

    positional arg (unlike AntigravitySDKGateway.model, which defaults to
    None and resolves via LocalAgentConfig/ADC/env) -- a live
    `--backend pydantic_native` run crashed with `TypeError:
    PydanticNativeSearchGateway.__init__() missing 1 required positional
    argument: 'model'` because default_deps()'s gateway_factory never
    passed it through. Confirms both backends now construct successfully
    from the same default_deps(model) call, and that AntigravitySDKGateway
    does NOT receive the pydantic_ai model string (a different namespace
    from the Antigravity SDK's own model identifiers).
    """
    deps = default_deps(TestModel())

    native_plan = ResearchPlan(question="q", retrieval_backend="pydantic_native", requests=[])
    native_gateway = deps.gateway_factory(native_plan)
    assert isinstance(native_gateway, PydanticNativeSearchGateway)

    antigravity_plan = ResearchPlan(question="q", retrieval_backend="antigravity", requests=[])
    antigravity_gateway = deps.gateway_factory(antigravity_plan)
    assert isinstance(antigravity_gateway, AntigravitySDKGateway)
    assert antigravity_gateway._model is None


def test_default_deps_threads_enable_otel_tracing_into_antigravity_backend_only():
    """enable_otel_tracing is meaningful only for AntigravitySDKGateway --

    PydanticNativeSearchGateway is already covered by logfire.instrument_
    pydantic_ai() (tracing.py), so it must not be passed there (it isn't a
    constructor kwarg PydanticNativeSearchGateway accepts).
    """
    deps = default_deps(TestModel(), enable_otel_tracing=True)

    antigravity_plan = ResearchPlan(question="q", retrieval_backend="antigravity", requests=[])
    antigravity_gateway = deps.gateway_factory(antigravity_plan)
    assert isinstance(antigravity_gateway, AntigravitySDKGateway)
    assert antigravity_gateway._enable_otel is True

    native_plan = ResearchPlan(question="q", retrieval_backend="pydantic_native", requests=[])
    native_gateway = deps.gateway_factory(native_plan)
    assert isinstance(native_gateway, PydanticNativeSearchGateway)
