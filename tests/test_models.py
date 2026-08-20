"""Contract & validation tests (PLAN.md Test Plan: "Contract & Validation Tests")."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from pydagy_research.models import (
    Citation,
    Claim,
    EvidenceRecord,
    ResearchAnswer,
    ResearchPlan,
    SearchOrFetchRequest,
    validate_research_answer,
)


def _evidence(evidence_id: str, source_kind: str = "page_content") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        source_url=f"https://example.com/{evidence_id}",
        source_kind=source_kind,
        title=f"Title {evidence_id}",
        raw_extract="some extracted text",
        timestamp=datetime.now(timezone.utc),
        status="success",
    )


def test_research_plan_caps_requests_at_five():
    with pytest.raises(ValidationError):
        ResearchPlan(
            question="q",
            requests=[
                SearchOrFetchRequest(request_id=f"r{i}", action="search", query_or_url="x")
                for i in range(6)
            ],
        )


def test_research_plan_defaults_to_antigravity_backend():
    plan = ResearchPlan(question="q", requests=[])
    assert plan.retrieval_backend == "antigravity"


def test_research_answer_rejects_citation_not_backing_any_claim():
    with pytest.raises(ValidationError):
        ResearchAnswer(
            answer="text",
            claims=[Claim(claim_text="c1", evidence_ids=["EVID-001"])],
            citations=[Citation(evidence_id="EVID-002", source_url="https://x", snippet="s")],
            limitations=[],
        )


def test_research_answer_allows_citation_backing_a_claim():
    answer = ResearchAnswer(
        answer="text",
        claims=[Claim(claim_text="c1", evidence_ids=["EVID-001"])],
        citations=[Citation(evidence_id="EVID-001", source_url="https://x", snippet="s")],
        limitations=[],
    )
    assert answer.citations[0].evidence_id == "EVID-001"


def test_validate_research_answer_accepts_grounded_page_content_citation():
    pool = {"EVID-001": _evidence("EVID-001", source_kind="page_content")}
    answer = ResearchAnswer(
        answer="text",
        claims=[Claim(claim_text="c1", evidence_ids=["EVID-001"])],
        citations=[Citation(evidence_id="EVID-001", source_url="https://x", snippet="s")],
        limitations=[],
    )
    assert validate_research_answer(answer, pool) == []


def test_validate_research_answer_rejects_unknown_evidence_id():
    pool: dict[str, EvidenceRecord] = {}
    answer = ResearchAnswer(
        answer="text",
        claims=[Claim(claim_text="c1", evidence_ids=["EVID-999"])],
        citations=[Citation(evidence_id="EVID-999", source_url="https://x", snippet="s")],
        limitations=[],
    )
    violations = validate_research_answer(answer, pool)
    assert any("Unknown evidence_id" in v for v in violations)


def test_validate_research_answer_rejects_citation_on_search_summary():
    pool = {"EVID-001": _evidence("EVID-001", source_kind="search_summary")}
    answer = ResearchAnswer(
        answer="text",
        claims=[Claim(claim_text="c1", evidence_ids=["EVID-001"])],
        citations=[Citation(evidence_id="EVID-001", source_url="https://x", snippet="s")],
        limitations=[],
    )
    violations = validate_research_answer(answer, pool)
    assert any("search_summary" in v for v in violations)
