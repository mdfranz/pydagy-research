"""Typed data contracts for the research pipeline (PLAN.md §5).

These are the pure-Pydantic boundary types shared by both retrieval gateway
backends (`AntigravitySDKGateway` and `PydanticNativeSearchGateway`) and by
every node in the `pydantic_graph` pipeline. Nothing in this module imports
either backend SDK, so it has zero runtime dependency on `google-antigravity`
or on any particular `pydantic_ai` model provider.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "SearchOrFetchRequest",
    "ResearchPlan",
    "EvidenceRecord",
    "Claim",
    "Citation",
    "ResearchAnswer",
    "ResearchReport",
    "validate_research_answer",
]

RetrievalBackend = Literal["antigravity", "pydantic_native"]
SourceKind = Literal["search_summary", "page_content"]


class SearchOrFetchRequest(BaseModel):
    """A single retrieval instruction the Planner Node hands to the gateway."""

    request_id: str
    action: Literal["search", "read"]
    query_or_url: str
    domain: str | None = None


class ResearchPlan(BaseModel):
    """Bounded plan produced by the Planner Node (PLAN.md §5, §6.1).

    `requests` is capped at 5 so a single Antigravity session's budget
    (PLAN.md §1.1) can be sized deterministically against the whole plan.
    """

    question: str
    retrieval_backend: RetrievalBackend = "antigravity"
    requests: list[SearchOrFetchRequest] = Field(max_length=5)


class EvidenceRecord(BaseModel):
    """One retrieved piece of evidence, tagged with its evidentiary quality.

    `source_kind` is what makes citation verification possible without
    trusting an inner model's own summary of its tool calls (PLAN.md §2):

    * "search_summary" — a `search_web` result. A single narrative string
      synthesized from multiple sources by the *retrieval* model. Useful for
      triage, never directly citable.
    * "page_content" — a `read_url_content` / `view_file` / native web-fetch
      result. One URL, one clean extract. The only kind the citation
      validator (`validate_research_answer` below) accepts.

    `provider` tags which retrieval backend or model produced this record.
    Used for multi-provider correlation and transparent fallback tracking.
    """

    evidence_id: str
    source_url: str
    source_kind: SourceKind
    title: str
    raw_extract: str
    timestamp: datetime
    status: Literal["success", "failed"] = "success"
    drift_flagged: bool = Field(
        default=False,
        description=(
            "Set by the retrieval gateway's post-tool-call drift check when the"
            " tool actually executed (query/url) diverged from what the"
            " SearchOrFetchRequest asked for (PLAN.md 'Known Constraints')."
        ),
    )
    provider: str | None = None
    error: str | None = None


class Claim(BaseModel):
    """One factual statement in the final answer, grounded in evidence."""

    claim_text: str
    evidence_ids: list[str] = Field(min_length=1)


class Citation(BaseModel):
    """One citation backing a claim, pointing at a specific evidence record."""

    evidence_id: str
    source_url: str
    snippet: str


class ResearchAnswer(BaseModel):
    """Final grounded answer produced by the Writer Node (PLAN.md §5, §6.4).

    The cheap, context-free invariants (referential integrity between
    `claims` and `citations` within the answer itself) are enforced here via
    `@model_validator`. The expensive invariants that require the runtime
    evidence pool — "every id actually exists" and "every citation rests on
    a page_content record" — cannot be expressed as a self-contained Pydantic
    validator (they need data the model class doesn't own), so they live in
    `validate_research_answer` below and are enforced twice: once as a
    `pydantic_ai` `@agent.output_validator` (fast, automatic model retry) and
    once as the graph's `CheckCitations` decision gate (PLAN.md §6 diagram).
    """

    answer: str
    claims: list[Claim]
    citations: list[Citation]
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _citations_reference_a_claim(self) -> "ResearchAnswer":
        claimed_ids = {eid for claim in self.claims for eid in claim.evidence_ids}
        orphan_citations = [c.evidence_id for c in self.citations if c.evidence_id not in claimed_ids]
        if orphan_citations:
            raise ValueError(
                "Citations must back at least one claim; evidence_ids cited but"
                f" never claimed: {sorted(set(orphan_citations))}"
            )
        return self


class ResearchReport(BaseModel):
    """Complete research result including the answer and operational telemetry.

    This wraps `ResearchAnswer` with source attempt and validation metadata
    (MULTI-PROVIDER-PLAN.md §5) to provide full transparency into the
    retrieval pipeline's behavior: which providers were consulted, which
    attempts succeeded/failed, and why records were filtered by the validator.
    """

    answer: ResearchAnswer
    source_attempts: list = Field(default_factory=list)
    validation_summary: dict | None = None
    dropped_records: list[tuple[dict, str]] = Field(default_factory=list)


def validate_research_answer(
    answer: ResearchAnswer, evidence_pool: dict[str, EvidenceRecord]
) -> list[str]:
    """Checks `answer` against the actual retrieved evidence pool.

    Returns a list of human-readable violation messages (empty list means the
    answer is fully grounded). This is the runtime-context-dependent half of
    the validation described in PLAN.md §5:

    1. every `Citation.evidence_id` and `Claim.evidence_ids` must map to a
       known id in `evidence_pool`, and
    2. every `Citation.evidence_id` must map to a `source_kind == "page_content"`
       record — a citation rested on a `"search_summary"` record fails.
    """
    violations: list[str] = []

    referenced_ids = {eid for claim in answer.claims for eid in claim.evidence_ids}
    referenced_ids |= {c.evidence_id for c in answer.citations}
    unknown_ids = sorted(eid for eid in referenced_ids if eid not in evidence_pool)
    if unknown_ids:
        violations.append(f"Unknown evidence_id(s) not in the retrieved evidence pool: {unknown_ids}")

    for citation in answer.citations:
        record = evidence_pool.get(citation.evidence_id)
        if record is None:
            continue  # already reported above
        if record.source_kind != "page_content":
            violations.append(
                f"Citation {citation.evidence_id} rests on a '{record.source_kind}' record"
                " (only 'page_content' evidence is citable)"
            )

    return violations
