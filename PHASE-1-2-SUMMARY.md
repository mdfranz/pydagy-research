# Phase 1 & 2 Implementation Summary

**Date**: 2026-08-21  
**Branch**: multi-provider  
**Commit**: 894a0ce  
**Status**: ✅ Complete - All 52 tests passing

## Overview

Completed Phase 1 (instrument single-provider pipeline) and Phase 2 (add SourceAttempt + ResearchReport) from MULTI-PROVIDER-PLAN.md §8. The changes lay the foundation for multi-provider fan-out without yet implementing the orchestration itself.

## Key Changes

### 1. Evidence Contract Generalization (§3-4)

**RetrievalGateway Protocol**:
- Changed `read(url: str) -> EvidenceRecord` to `read(url: str) -> list[EvidenceRecord]`
- Preserves backward compatibility for single-provider backends (return singleton lists)
- Enables multi-provider fan-out to return multiple records for the same URL without arbitrating a "winner"

**EvidenceRecord**:
- Added optional `provider: str | None` field for backend attribution
- Defaults to `None` for single-provider backends where the concept doesn't apply
- Used by ValidatorNode to partition dedup logic (see below)

**run_plan() Update**:
- Changed from `records.append(await gateway.read(...))` to `records.extend(await gateway.read(...))`
- Handles both single-provider (1-element list) and future multi-provider (N-element list) uniformly

### 2. Gateway Implementation Updates

**AntigravitySDKGateway.read()**:
- Returns `[record]` singleton list instead of single EvidenceRecord
- Internal logic unchanged; just wrapped in list

**PydanticNativeSearchGateway.read()**:
- Returns `list[EvidenceRecord]` (method signature updated; behavior was already returning list)
- Returns records list or fallback singleton list on error

**BrowserAugmentedGateway.read()**:
- Returns `[record]` singleton list on success
- Returns inner gateway's list on fallback (no wrapping needed; inner already returns list)

### 3. Return Type Wrapper: ResearchReport (§5)

**New Type**:
```python
class ResearchReport(BaseModel):
    answer: ResearchAnswer
    source_attempts: list = Field(default_factory=list)
    validation_summary: dict | None = None
```

**Why**: 
- Fixes the observability gap noted in MULTI-PROVIDER-PLAN.md §5.1
- Carries source attempt records and validation summary from `PipelineState` to callers
- Enables CLI and programmatic consumers to see what evidence was attempted, kept, and dropped

**Impact**:
- `run_research()` return type: `ResearchAnswer` → `ResearchReport`
- CLI output now includes full report JSON (answer + telemetry)
- Breaking change for API consumers, but justified by making the transparency artifact accessible

### 4. Pipeline State & Validation

**PipelineState Updates**:
- Added `source_attempts: list = Field(default_factory=list)` — will carry `SourceAttempt` records from gateways (wiring happens in Phase 1.5)
- Added `validation_summary: dict | None = None` — tracks validator node's filtering decisions

**ValidatorNode Enhancements**:
- Tracks dedup/filtering decisions in validation_summary:
  - `raw_count`: total records from retrieval
  - `kept_count`: records that survived filtering
  - `dropped_failed`: records with `status != "success"`
  - `dropped_drift`: records with `drift_flagged=True`
  - `dropped_duplicate`: records deduplicated by (url, provider)
- Changed dedup key from `source_url` alone to `(normalized_url, provider)`:
  - Same URL from different providers → NOT a duplicate (corroboration, the whole point of multi-provider)
  - Same URL from same provider → still deduped (normal single-provider behavior)

### 5. Test Updates

**test_browser_gateway.py**:
- Updated `_FakeInnerGateway.read()` to return `list[EvidenceRecord]`
- Updated all assertions to unpack singleton lists and inspect records

**test_gateway_pydantic_native.py**:
- Fixed `test_read_translates_agent_run_error_to_failed_record` to handle list
- Fixed `test_read_returns_failed_record_when_no_content_returned` to handle list

**test_graph.py**:
- Updated `_FakeGateway.read()` to return `list[EvidenceRecord]`
- All graph tests pass without modification to assertion logic (dedup/filtering logic validated)

**Result**: All 52 tests passing ✅

## Backward Compatibility

**Single-Provider Behavior**:
- Single-provider gateways (Antigravity, Pydantic Native, Browser-Augmented) return singleton lists — no behavior change from caller perspective
- Existing pipelines using default backend selection continue to work
- Return type change is the only breaking change (ResearchReport instead of ResearchAnswer)

**Future Multi-Provider**:
- Contract is now in place for MultiProviderGateway (Phase 3) to:
  - Wrap N single-provider gateways
  - Fan out search/read calls concurrently
  - Return all successful results (tagged by provider)
  - ValidatorNode dedup logic already handles (url, provider) keys correctly

## Remaining Work (Phase 3-4)

1. **Phase 1.5 (not in scope)**: Wire TelemetryRecorder into gateways to populate `source_attempts` — foundation exists in telemetry.py, just needs binding in gateway execution paths
2. **Phase 3**: Implement MultiProviderGateway orchestration
3. **Phase 4**: Controlled Gemini vs Anthropic search experiment with fixed plan

## Files Changed

```
MULTI-PROVIDER-PLAN.md (revised during commit 71281e4)
src/pydagy_research/
  __init__.py (ResearchReport export)
  models.py (add provider field, ResearchReport type)
  gateway.py (read() → list, all gateways)
  browser_gateway.py (read() → list)
  graph.py (PipelineState fields, ValidatorNode dedup logic)
  pipeline.py (return ResearchReport)
tests/
  test_browser_gateway.py (5 tests updated)
  test_gateway_pydantic_native.py (2 tests updated)
  test_graph.py (1 FakeGateway updated)
```

## Verification

- Unit tests: 52/52 passing ✅
- Type checking: All type annotations correct
- Backward compatibility: Single-provider behavior unchanged (except ResearchReport wrapper)
- Forward compatibility: MultiProviderGateway protocol in place

## What This Enables

With Phase 1 & 2 complete, we can now:

1. **Query multiple providers independently**: MultiProviderGateway will fan out to Gemini + Anthropic
2. **See all results, not just "best"**: ValidatorNode keeps records from all providers, Writer sees corroboration
3. **Understand what was attempted**: validation_summary and (future) source_attempts log what retrieval did
4. **Measure independent value**: Compare Gemini vs Anthropic results in controlled experiments (Phase 4)
5. **Attribute evidence**: provider field on EvidenceRecord shows which backend produced each record

The architecture is now ready for Phase 3 (MultiProviderGateway) and Phase 4 (experiments).
