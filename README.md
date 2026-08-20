# pydagy-research

A Pydantic-first research pipeline that uses either the Google Antigravity
SDK or Pydantic AI's native web search/fetch as a swappable, sandboxed
retrieval backend behind a typed `pydantic_graph` pipeline. See
[`PLAN.md`](PLAN.md) for the full architecture and design rationale.

```
Planner Node -> Retrieval Gateway Node -> Evidence Validator Node -> Writer Node -> ResearchAnswer
                (Antigravity SDK or                                   (loops back on failed
                 pydantic_ai WebSearch/WebFetch)                       citation grounding)
```

## Setup

```bash
uv sync --extra antigravity   # extra only needed for the "antigravity" backend
```

## Usage

```python
import asyncio
from pydagy_research import run_research

answer = asyncio.run(
    run_research(
        "What is the latest stable Python release?",
        model="google:gemini-3.7-flash",
        retrieval_backend="antigravity",  # or "pydantic_native"
    )
)
print(answer.answer)
for citation in answer.citations:
    print(f"- {citation.snippet} ({citation.source_url})")
```

Or from the CLI (requires a configured model provider, e.g. `GEMINI_API_KEY`):

```bash
uv run pydagy-research "What is the latest stable Python release?"
```

## Package layout

- `models.py` — the typed contracts (`ResearchPlan`, `EvidenceRecord`, `ResearchAnswer`, ...) from PLAN.md §5.
- `gateway.py` — the `RetrievalGateway` protocol and both backends (`AntigravitySDKGateway`, `PydanticNativeSearchGateway`) from PLAN.md §1.
- `agents.py` — the Planner and Writer `pydantic_ai` agents from PLAN.md §6.1/§6.4, including the evidence-grounding output validator.
- `graph.py` — the `pydantic_graph` state machine from PLAN.md §6.
- `pipeline.py` — the `run_research()` convenience entrypoint.

## Tests

```bash
uv run pytest
```

All tests are offline: no `localharness` binary or live model credentials
are required. The Antigravity gateway's hook-based evidence extraction and
drift check are exercised directly against real `google.antigravity.types`
objects; the graph tests use `pydantic_ai.models.test.TestModel` with a fake
`RetrievalGateway`. Live integration/benchmark tests described in PLAN.md's
Test Plan (real search/fetch calls, backend comparison) are not included
here — they need real credentials and are out of scope for this offline
suite.
