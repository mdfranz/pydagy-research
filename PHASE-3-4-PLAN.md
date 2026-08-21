# Phase 3 & 4 Work Plan

**Status**: Ready to implement  
**Depends on**: Phase 1 & 2 ✅ (894a0ce)

## Phase 3: MultiProviderGateway Implementation

### 3.1 Core Orchestration (MULTI-PROVIDER-PLAN.md §3)

Implement `MultiProviderGateway` class:

```python
class MultiProviderGateway:
    def __init__(self, gateways: dict[str, RetrievalGateway]) -> None:
        self._gateways = gateways
        # read_capable tracks which providers support WebFetch
        self._read_capable = {name for name, gw in gateways.items() if has_read_capability(gw)}
        self.attempts: list[SourceAttempt] = []
    
    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
        """Fan out to all providers concurrently, tag results, return all."""
        # asyncio.gather all providers' search() calls
        # Concatenate all results, tagging each with provider
        
    async def read(self, url: str) -> list[EvidenceRecord]:
        """Fan out to read-capable providers concurrently."""
        # Only call _read_capable providers (OpenAI excluded unless local_fetch fallback)
        # Tag each with provider
        # Record all attempts (success/failure) in self.attempts
        # Return ALL successful records (no arbitration)
```

**Key Design Points**:
- Generic over any set of RetrievalGateway instances
- `search()` fans out to ALL providers (different search indices)
- `read()` fans out only to `_read_capable` providers (OpenAI has WebSearch but not WebFetch)
- Concurrent execution: `asyncio.gather(..., return_exceptions=True)`
- No arbitration: keep every successful provider's record
- Transparent failure: record every attempt (success/failed/thin) for visibility

### 3.2 Attempt Recording Integration

Wire SourceAttempt recording into MultiProviderGateway and child gateways:

```python
# In each provider's read/search:
attempt = SourceAttempt(
    request_id=...,  # thread from caller
    provider="gemini" or "anthropic" or "openai",
    action="search" or "read",
    query_or_url=...,
    tier=determine_tier(record),  # "verified", "thin", "triage_only", "excluded"
    status="success" or "failed",
    char_count=len(record.raw_extract),
    duration_ms=elapsed,
    extraction_method=infer_extraction_method(),
    model=self._model if applicable,
    drift_flagged=record.drift_flagged,
    error=record.error if failed,
)
self.attempts.append(attempt)
```

**Integration Points**:
- Each gateway maintains `self.attempts: list[SourceAttempt]`
- MultiProviderGateway combines child attempts from all providers
- RetrievalNode copies combined attempts into `PipelineState.source_attempts`
- ResearchReport carries attempts for CLI/API visibility

### 3.3 Test Coverage

Add tests for MultiProviderGateway:

```
test_multi_provider_gateway.py:
- test_search_fans_out_to_all_providers()
- test_read_fans_out_only_to_read_capable()
- test_read_skips_write_only_providers_by_default()
- test_read_with_local_fetch_fallback_for_open_ai()
- test_returns_all_successful_records_not_single_best()
- test_records_all_attempts_success_and_failure()
- test_concurrent_fan_out_performance()
- test_timeout_and_retry_behavior()
```

## Phase 4: Controlled Multi-Provider Search Experiment

### 4.1 Fixed Plan Experiment (MULTI-PROVIDER-PLAN.md §1.1, §8)

Run the same ResearchPlan through multiple providers independently:

**Setup**:
```python
plan = ResearchPlan(
    question="...",
    requests=[
        SearchOrFetchRequest(...),
        SearchOrFetchRequest(...),
    ]
)
plan_hash = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()

# Fix the Planner output so both providers solve identical retrieval subproblems
# (not re-running Planner for each backend, which would change the workload
# before retrieval begins — not a fair comparison)
```

**Execution**:
```python
for provider_name, model_id in [("gemini", "google:gemini-3.7-flash"), 
                                 ("anthropic", "anthropic:claude-opus-5")]:
    experiment_id = f"provider-comparison-{plan_hash[:8]}"
    
    report_gemini = await run_research(
        question=plan.question,
        model=model_id,
        # Force use of this provider only (no multi-provider fan-out yet)
    )
    
    # Store results for comparison
```

**Metrics to Collect** (in SourceAttempt + validation_summary):
- Search query overlap vs divergence (did they search the same things?)
- Read URL overlap vs divergence (did they fetch the same pages?)
- Result uniqueness: unique facts each provider found that the other didn't
- Latency: wall time per provider (expect sub-second per call due to concurrency)
- Token cost: normalize across provider pricing models
- Evidence quality: did multi-provider (future) produce different/better citations?

### 4.2 Live Comparison Workflow

**1. Collect baseline data**:
- Run the existing 9 traces through analysis to get Gemini + Gemini metrics
- (Today's backend comparison was Gemini vs Gemini, not actual provider independence test)

**2. Run controlled Gemini vs Anthropic test**:
- 3-5 fixed research plans
- Same Planner output for both
- Same retrieval requests
- Compare results in Logfire

**3. Document findings** in FINDINGS.md §4:
- Did Anthropic + Gemini hit different search indices, or the same Google?
- Did they fetch the same pages, or discover different sources?
- Did Writer see genuinely different evidence that changed the answer?
- Cost/latency tradeoff: worth the additional model calls?

**4. Decision gate**:
- If independence/value is not demonstrated: reconsider design
- If confirmed: proceed to Phase 5 (MultiProviderGateway.search() fan-out)

### 4.3 Test Coverage

```
test_multi_provider_comparison.py:
- test_run_same_plan_through_gemini_and_anthropic()
- test_compare_search_queries_for_overlap()
- test_compare_fetched_urls_for_overlap()
- test_measure_unique_facts_per_provider()
- test_validate_independence_premise()
```

Also: integration test running full end-to-end with both providers:

```
test_multi_provider_e2e.py:
- test_multi_provider_run_with_gemini_and_anthropic()
  (this will exercise MultiProviderGateway and measure the results)
```

## Milestones & Checkpoints

| Phase | Checkpoint | Criteria |
|-------|-----------|----------|
| **3.0** | MultiProviderGateway skeleton | Class exists, search/read signatures match protocol |
| **3.1** | Fan-out orchestration | Both methods call all providers concurrently |
| **3.2** | Attempt recording | SourceAttempt objects populate attempts list |
| **3.3** | Integration with graph | PipelineState.source_attempts populated, ResearchReport carries it |
| **3.4** | Unit tests | 10+ tests passing for MultiProviderGateway |
| **4.0** | Baseline Gemini data | 3+ runs through existing test plans, results logged |
| **4.1** | Anthropic data | Same 3+ plans run with Anthropic model |
| **4.2** | Comparison analysis | Uniqueness, overlap, latency, cost metrics calculated |
| **4.3** | Independence decision | Decision made: proceed with fan-out or revise design |
| **4.4** | Documentation | FINDINGS.md §4 documents results and decision rationale |

## Known Unknowns

1. **Search index independence**: Do Gemini's WebSearch and Anthropic's WebSearch hit the same Google index or different ones? (Likely same per pydantic_ai implementation, but unverified)

2. **Latency impact**: Will concurrent fan-out add significant tail latency due to slowest provider, or stay close to single-provider baseline?

3. **Cost/value tradeoff**: Is 2-3x cost of multi-provider calls justified by better evidence corroboration?

4. **Rate limiting**: Will multiple providers per request hit API rate limits in practice? Need to test at scale.

## Open Questions for Design Review

1. Should MultiProviderGateway cache results per URL to avoid refetching the same page from both providers? (Cost optimization vs transparency)

2. How should OpenAI be configured by default? (Currently: search-only, no read capability unless local_fetch fallback added explicitly)

3. Should there be a fan-out size limit or cost cap? (e.g., max 2 providers, max $0.10 per request)

4. How do we handle provider API degradation/timeout? (Best-effort continue with other providers, or fail fast?)

## Implementation Order

1. **MultiProviderGateway class & protocol** (Core orchestration)
2. **SourceAttempt recording** (Telemetry wiring)
3. **Graph integration** (PipelineState threading)
4. **Unit tests for MultiProviderGateway** (Confidence)
5. **Integration test end-to-end** (Full system validation)
6. **Controlled experiment harness** (Reproducible baseline)
7. **Gemini + Anthropic comparison** (The real test)
8. **Document findings & decision** (FINDINGS.md update)
