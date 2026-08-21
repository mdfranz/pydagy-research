"""Structured retrieval telemetry contracts and Logfire emission helpers.

This module intentionally has no dependency on the graph, gateway, browser,
or public ``run_research()`` entrypoint. It is the phase-zero foundation for
the telemetry described in ``MULTI-PROVIDER-PLAN.md`` §5.1; wiring its
``TelemetryRecorder`` into those execution paths is a later change.

The emitter imports Logfire only when explicitly enabled. That keeps ordinary
library imports and tests free of process-wide tracing side effects while
allowing callers that already configured Logfire to emit structured spans.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "ExperimentContext",
    "RetrievalRequestTelemetry",
    "SourceAttempt",
    "ValidationSummary",
    "TelemetryEmitter",
    "TelemetryRecorder",
]

AttemptAction = Literal["search", "read"]
AttemptStatus = Literal["success", "failed"]
EvidenceTier = Literal["verified", "thin", "triage_only", "excluded"]
ExtractionMethod = Literal[
    "browser",
    "native_tool",
    "antigravity_tool",
    "local_fetch",
    "response_text_fallback",
    "unknown",
]


class ExperimentContext(BaseModel):
    """Stable metadata shared by every span in one controlled experiment."""

    experiment_id: str | None = None
    scenario: str | None = None
    plan_hash: str | None = None
    configured_backends: list[str] = Field(default_factory=list)
    configured_models: list[str] = Field(default_factory=list)
    browser_enabled: bool = False

    def span_attributes(self) -> dict[str, Any]:
        """Return Logfire-safe, consistently named experiment attributes."""
        return _without_none(
            {
                "research.experiment.id": self.experiment_id,
                "research.scenario": self.scenario,
                "research.plan.hash": self.plan_hash,
                "research.backends": self.configured_backends,
                "research.models": self.configured_models,
                "research.browser.enabled": self.browser_enabled,
            }
        )


class RetrievalRequestTelemetry(BaseModel):
    """The parent span metadata for one logical retrieval request."""

    request_id: str = Field(min_length=1)
    action: AttemptAction
    query_or_url: str = Field(min_length=1)
    fanout_size: int = Field(default=1, ge=1)

    def span_attributes(self) -> dict[str, Any]:
        return {
            "retrieval.request.id": self.request_id,
            "retrieval.action": self.action,
            "retrieval.target": self.query_or_url,
            "retrieval.fanout.size": self.fanout_size,
        }


class SourceAttempt(BaseModel):
    """One browser or provider attempt made for a logical retrieval request."""

    request_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    action: AttemptAction
    query_or_url: str = Field(min_length=1)
    tier: EvidenceTier
    status: AttemptStatus
    char_count: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    extraction_method: ExtractionMethod = "unknown"
    model: str | None = None
    evidence_id: str | None = None
    drift_flagged: bool = False
    note: str = ""
    error: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_outcome(self) -> "SourceAttempt":
        if self.status == "failed" and self.tier != "excluded":
            raise ValueError("Failed attempts must have tier='excluded'")
        if self.evidence_id is not None and self.tier == "excluded":
            raise ValueError("Excluded attempts cannot have an evidence_id")
        if self.evidence_id is not None and self.status != "success":
            raise ValueError("Only successful attempts can have an evidence_id")
        return self

    def span_attributes(self) -> dict[str, Any]:
        """Return attempt metadata without putting data into a span template."""
        return _without_none(
            {
                "retrieval.request.id": self.request_id,
                "retrieval.provider": self.provider,
                "retrieval.model": self.model,
                "retrieval.action": self.action,
                "retrieval.target": self.query_or_url,
                "retrieval.status": self.status,
                "retrieval.tier": self.tier,
                "retrieval.char_count": self.char_count,
                "retrieval.duration_ms": self.duration_ms,
                "retrieval.extraction_method": self.extraction_method,
                "retrieval.drift_flagged": self.drift_flagged,
                "retrieval.evidence_id": self.evidence_id,
                "retrieval.note": self.note or None,
                "retrieval.error": self.error,
                "retrieval.timestamp": self.timestamp,
            }
        )


class ValidationSummary(BaseModel):
    """Counts and reasons emitted after ValidatorNode finishes one pool."""

    raw_count: int = Field(ge=0)
    kept_count: int = Field(ge=0)
    dropped_failed: int = Field(default=0, ge=0)
    dropped_drift: int = Field(default=0, ge=0)
    dropped_duplicate: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _counts_reconcile(self) -> "ValidationSummary":
        dropped = self.dropped_failed + self.dropped_drift + self.dropped_duplicate
        if self.raw_count != self.kept_count + dropped:
            raise ValueError("raw_count must equal kept_count plus all dropped counts")
        return self

    def span_attributes(self) -> dict[str, int]:
        return {
            "retrieval.validation.raw_count": self.raw_count,
            "retrieval.validation.kept_count": self.kept_count,
            "retrieval.validation.dropped_failed": self.dropped_failed,
            "retrieval.validation.dropped_drift": self.dropped_drift,
            "retrieval.validation.dropped_duplicate": self.dropped_duplicate,
        }


class SpanFactory(Protocol):
    """Subset of ``logfire.span`` used by ``TelemetryEmitter``."""

    def __call__(self, msg_template: str, /, **attributes: Any) -> Any: ...


class TelemetryEmitter:
    """Emit structured Logfire spans, or no-op when telemetry is disabled."""

    def __init__(self, *, enabled: bool = False, span_factory: SpanFactory | None = None) -> None:
        self._span_factory = span_factory or (_logfire_span if enabled else None)

    @contextmanager
    def span(self, name: str, /, **attributes: Any) -> Iterator[Any | None]:
        """Open a span only when an enabled/injected span factory is available."""
        if self._span_factory is None:
            with nullcontext(None) as span:
                yield span
            return
        with self._span_factory(name, **attributes) as span:
            yield span


class TelemetryRecorder:
    """Collect structured telemetry locally and emit it to Logfire when enabled.

    Keeping the records locally gives a future ``ResearchReport`` a single
    source of truth. Emitting a span at recording time preserves the same data
    for operational queries without requiring prompt-payload inspection.
    """

    def __init__(
        self,
        *,
        experiment: ExperimentContext | None = None,
        emitter: TelemetryEmitter | None = None,
    ) -> None:
        self.experiment = experiment or ExperimentContext()
        self.emitter = emitter or TelemetryEmitter()
        self.attempts: list[SourceAttempt] = []
        self.validation_summaries: list[ValidationSummary] = []

    @contextmanager
    def request_span(self, request: RetrievalRequestTelemetry) -> Iterator[Any | None]:
        """Create the parent span for a future gateway request implementation."""
        attributes = self.experiment.span_attributes() | request.span_attributes()
        with self.emitter.span("research retrieval request", **attributes) as span:
            yield span

    def record_attempt(self, attempt: SourceAttempt) -> None:
        """Append and emit one completed browser/provider attempt."""
        self.attempts.append(attempt)
        attributes = self.experiment.span_attributes() | attempt.span_attributes()
        with self.emitter.span("research retrieval attempt", **attributes):
            pass

    def record_validation(self, summary: ValidationSummary) -> None:
        """Append and emit the ValidatorNode's decision counts."""
        self.validation_summaries.append(summary)
        attributes = self.experiment.span_attributes() | summary.span_attributes()
        with self.emitter.span("research evidence validation", **attributes):
            pass


def _logfire_span(msg_template: str, /, **attributes: Any) -> Any:
    """Import Logfire only when an emitter was explicitly enabled."""
    import logfire

    return logfire.span(msg_template, **attributes)


def _without_none(attributes: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attributes.items() if value is not None}
