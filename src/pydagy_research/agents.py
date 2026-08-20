"""`pydantic_ai` agent factories for the Planner and Writer nodes (PLAN.md §6).

Kept separate from `graph.py` so the graph module stays agnostic to how the
agents are built — tests wire `TestModel`/`FunctionModel` in here directly
(PLAN.md Test Plan: "Use TestModel / FunctionModel to test Planner,
Validator, and Writer nodes deterministically").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import WebSearch
from pydantic_ai.exceptions import ModelRetry

from .models import EvidenceRecord, ResearchAnswer, ResearchPlan, validate_research_answer

__all__ = ["WriterDeps", "build_planner_agent", "build_writer_agent", "planner_prompt", "writer_prompt"]


PLANNER_SYSTEM_PROMPT = """\
You are the Planner Node of a grounded research pipeline. Given a user's \
question, produce a bounded ResearchPlan: at most 5 SearchOrFetchRequest \
entries, each either a "search" (a Google-style search query) or a "read" \
(a specific URL to fetch).

The plan you output is executed exactly as written and only once — the \
Retrieval Node that runs after you has no way to see what a "read" turned \
up and go pick a better URL; whatever you put in `query_or_url` is final. \
Only "read" (page_content) evidence can ever be cited; a bare "search" \
result can never be cited, no matter how good the summary looks. This \
means a plan of search requests alone is USELESS for answering the \
question — it will produce zero citable evidence and the pipeline will be \
forced to give up rather than answer.

So: for every sub-question that must end up cited in the final answer, you \
MUST include at least one "read" request pointing at a specific, real URL \
— not a search query, and not a generic index/listing page when a more \
specific page is what actually has the answer. You have your own web \
search available *right now*, before you finalize the plan — use it to \
find the single specific, current page, not just a plausible-looking \
index. This matters most for anything versioned, numbered, or frequently \
updated (a specific security advisory, a specific release's notes, a \
specific CVE record): a vendor's top-level "/security/advisories/" index \
page lists advisory names but usually omits the actual CVE numbers or \
technical detail that live on each advisory's own page — reading the \
index alone will not get you a citable answer to "what CVE was this."

Pair each "read" with a "search" request on the same topic for triage / \
cross-checking, but never submit a plan that has "read" requests for \
zero of its topics.

Example — question: "What is the latest stable Python release?"
  1. {"action": "read", "query_or_url": "https://www.python.org/downloads/"}
  2. {"action": "search", "query_or_url": "latest stable Python release version"}

Example — question: "What CVEs were recently fixed in Firefox?" (an index
page is not enough here — use your search to find the specific advisory):
  1. {"action": "read", "query_or_url": "https://www.mozilla.org/en-US/security/advisories/mfsa2026-74/"}
  2. {"action": "search", "query_or_url": "Firefox 154 security advisory CVE"}
"""

WRITER_SYSTEM_PROMPT = """\
You are the Writer Node of a grounded research pipeline. You will be given \
the user's question and a pool of retrieved EvidenceRecords, each with a \
stable evidence_id, a source_kind ("search_summary" or "page_content"), and \
its raw extracted text. Write a ResearchAnswer:

- Every Claim must cite at least one evidence_id from the pool.
- Every Citation must point at a "page_content" evidence_id — never at a \
"search_summary" one, since a search summary is an unverified multi-source \
blob, not something you may present as a citable source.
- If the evidence pool is insufficient to answer some part of the question, \
say so explicitly in `limitations` rather than guessing.
- Never invent an evidence_id, a URL, or a quote that isn't in the pool."""


def planner_prompt(question: str) -> str:
    return f"User question: {question}\n\nProduce the ResearchPlan."


def writer_prompt(question: str, evidence_pool: dict[str, EvidenceRecord]) -> str:
    if not evidence_pool:
        lines = ["(no evidence was successfully retrieved)"]
    else:
        lines = [
            f"- {rec.evidence_id} [{rec.source_kind}] {rec.title} <{rec.source_url}>\n"
            f"  {rec.raw_extract[:2000]}"
            for rec in evidence_pool.values()
        ]
    evidence_block = "\n".join(lines)
    return f"User question: {question}\n\nEvidence pool:\n{evidence_block}\n\nProduce the ResearchAnswer."


def build_planner_agent(model: Any, *, enable_search: bool = True) -> Agent[None, ResearchPlan]:
    """Builds the Planner Node's agent (PLAN.md §6.1).

    `enable_search=True` (the default) gives the Planner its own `WebSearch`
    capability so it can look up a real, current, *specific* URL before
    committing to the plan — the plan is generated in one shot with no
    chance to revise a "read" target after the fact (PLAN.md's static
    graph has no back-edge from Retrieval to Planner), so a wrong guess
    here (e.g. a vendor's generic advisory index instead of the one
    advisory page that actually has the CVE numbers) can't be corrected
    downstream by any amount of better reading/rendering. Set to False for
    `TestModel`-based tests: `TestModel` raises `UserError` on any
    configured capability regardless of whether it's actually invoked.

    This search is planning-time grounding only — its results never become
    `EvidenceRecord`s or citable evidence; only the Retrieval Node's own
    `search()`/`read()` calls (PLAN.md §1) produce those.
    """
    capabilities = [WebSearch()] if enable_search else []
    return Agent(
        model,
        name="planner_agent",
        output_type=ResearchPlan,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        capabilities=capabilities,
    )


@dataclass
class WriterDeps:
    """Run-time dependency carrying the validated evidence pool (PLAN.md §6.4).

    Passed via `agent.run(prompt, deps=WriterDeps(...))` rather than baked
    into the Agent at construction time, since the evidence pool is only
    known once the Retrieval/Validator nodes have run.
    """

    evidence_pool: dict[str, EvidenceRecord]


def build_writer_agent(model: Any) -> Agent[WriterDeps, ResearchAnswer]:
    """Builds the Writer Node's agent, with grounding enforced as an output validator.

    This is the runtime-context-dependent half of the `ResearchAnswer`
    validation described in PLAN.md §5 (see `models.validate_research_answer`
    for why it can't live on the model class itself): every evidence_id must
    exist in `ctx.deps.evidence_pool`, and every citation must rest on a
    "page_content" record. A violation raises `ModelRetry`, which
    `pydantic_ai` uses to automatically re-prompt the model — the graph-level
    `WriterNode` (PLAN.md §6 diagram's `CheckCitations` gate) is the outer,
    bounded backstop once those in-agent retries are exhausted.
    """
    agent: Agent[WriterDeps, ResearchAnswer] = Agent(
        model,
        name="writer_agent",
        deps_type=WriterDeps,
        output_type=ResearchAnswer,
        system_prompt=WRITER_SYSTEM_PROMPT,
    )

    @agent.output_validator
    async def _enforce_grounding(ctx: RunContext[WriterDeps], output: ResearchAnswer) -> ResearchAnswer:
        violations = validate_research_answer(output, ctx.deps.evidence_pool)
        if violations:
            raise ModelRetry("; ".join(violations))
        return output

    return agent
