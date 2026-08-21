"""Tests for the isolated structured telemetry foundation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from pydagy_research.telemetry import (
    ExperimentContext,
    RetrievalRequestTelemetry,
    SourceAttempt,
    TelemetryEmitter,
    TelemetryRecorder,
    ValidationSummary,
)


class _FakeSpan:
    def __init__(self, calls: list[tuple[str, dict[str, object]]], name: str, attributes: dict[str, object]) -> None:
        self._calls = calls
        self._name = name
        self._attributes = attributes

    def __enter__(self) -> "_FakeSpan":
        self._calls.append((self._name, self._attributes))
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _attempt(**overrides: object) -> SourceAttempt:
    values: dict[str, object] = {
        "request_id": "req-1",
        "provider": "gemini",
        "action": "read",
        "query_or_url": "https://example.com/page",
        "tier": "verified",
        "status": "success",
        "char_count": 512,
        "duration_ms": 42.5,
        "extraction_method": "native_tool",
        "timestamp": datetime(2026, 8, 21, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SourceAttempt(**values)


def test_source_attempt_rejects_failed_attempt_without_excluded_tier():
    with pytest.raises(ValidationError, match="tier='excluded'"):
        _attempt(status="failed", tier="thin")


def test_validation_summary_requires_counts_to_reconcile():
    with pytest.raises(ValidationError, match="raw_count must equal"):
        ValidationSummary(raw_count=3, kept_count=1, dropped_failed=1)


def test_disabled_emitter_is_a_noop_and_recorder_retains_data():
    recorder = TelemetryRecorder()

    with recorder.request_span(
        RetrievalRequestTelemetry(request_id="req-1", action="read", query_or_url="https://example.com/page")
    ) as span:
        assert span is None

    attempt = _attempt()
    summary = ValidationSummary(raw_count=1, kept_count=1)
    recorder.record_attempt(attempt)
    recorder.record_validation(summary)

    assert recorder.attempts == [attempt]
    assert recorder.validation_summaries == [summary]


def test_recorder_emits_queryable_experiment_request_attempt_and_validation_attributes():
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_span(name: str, /, **attributes: object) -> _FakeSpan:
        return _FakeSpan(calls, name, attributes)

    recorder = TelemetryRecorder(
        experiment=ExperimentContext(
            experiment_id="gemini-anthropic-001",
            scenario="fixed-nginx-apache",
            plan_hash="abc123",
            configured_backends=["pydantic_native"],
            configured_models=["google:gemini-3.7-flash"],
            browser_enabled=True,
        ),
        emitter=TelemetryEmitter(span_factory=fake_span),
    )
    request = RetrievalRequestTelemetry(
        request_id="req-1",
        action="read",
        query_or_url="https://example.com/page",
        fanout_size=2,
    )

    with recorder.request_span(request):
        pass
    recorder.record_attempt(_attempt(model="google:gemini-3.7-flash", evidence_id="EVID-001"))
    recorder.record_validation(ValidationSummary(raw_count=2, kept_count=1, dropped_duplicate=1))

    request_name, request_attributes = calls[0]
    assert request_name == "research retrieval request"
    assert request_attributes["research.experiment.id"] == "gemini-anthropic-001"
    assert request_attributes["research.plan.hash"] == "abc123"
    assert request_attributes["research.browser.enabled"] is True
    assert request_attributes["retrieval.fanout.size"] == 2

    attempt_name, attempt_attributes = calls[1]
    assert attempt_name == "research retrieval attempt"
    assert attempt_attributes["retrieval.provider"] == "gemini"
    assert attempt_attributes["retrieval.duration_ms"] == 42.5
    assert attempt_attributes["retrieval.evidence_id"] == "EVID-001"

    validation_name, validation_attributes = calls[2]
    assert validation_name == "research evidence validation"
    assert validation_attributes["retrieval.validation.dropped_duplicate"] == 1
