"""Tests for repeatable fixed-plan provider comparisons without live APIs."""

from __future__ import annotations

from datetime import datetime, timezone

from pydagy_research import experiments
from pydagy_research.models import EvidenceRecord, ResearchAnswer, ResearchPlan, ResearchReport, SearchOrFetchRequest
from pydagy_research.telemetry import SourceAttempt


class _Gateway:
    def __init__(self, provider: str, recorder) -> None:
        self.provider = provider
        self.recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def search(self, query: str, domain: str | None = None):
        return [self._record("search", f"search:{query}")]

    async def read(self, url: str):
        return [self._record("read", url)]

    def _record(self, action: str, url: str) -> EvidenceRecord:
        self.recorder.record_attempt(
            SourceAttempt(
                request_id=f"{self.provider}-{action}", provider=self.provider,
                action=action, query_or_url=url, tier="verified", status="success",
                char_count=100, duration_ms=1, extraction_method="native_tool",
            )
        )
        return EvidenceRecord(
            evidence_id=f"RAW-{self.provider}-{action}", source_url=url,
            source_kind="search_summary" if action == "search" else "page_content",
            title=url, raw_extract="evidence", timestamp=datetime.now(timezone.utc),
            provider=self.provider,
        )


async def test_fixed_plan_experiment_runs_individual_baselines_and_fanout(monkeypatch):
    def fake_make_gateway(plan, *, model, telemetry_provider, telemetry_recorder):
        return _Gateway(telemetry_provider, telemetry_recorder)

    def fake_make_multi(plan, providers, model_map, *, telemetry_recorder):
        class _Multi(_Gateway):
            async def search(self, query, domain=None):
                return [self._for(provider, "search", f"search:{query}") for provider in providers]

            async def read(self, url):
                return [self._for(provider, "read", url) for provider in providers]

            def _for(self, provider, action, url):
                original = self.provider
                self.provider = provider
                try:
                    return self._record(action, url)
                finally:
                    self.provider = original

        return _Multi("fanout", telemetry_recorder)

    monkeypatch.setattr(experiments, "make_gateway", fake_make_gateway)
    monkeypatch.setattr(experiments, "make_multi_provider_gateway", fake_make_multi)
    plan = ResearchPlan(
        question="q",
        requests=[
            SearchOrFetchRequest(request_id="s", action="search", query_or_url="q"),
            SearchOrFetchRequest(request_id="r", action="read", query_or_url="https://example.com"),
        ],
    )

    result = await experiments.run_fixed_plan_experiment(
        plan,
        scenario="fixed-test",
        model_map={"gemini": "google:gemini-3.7-flash", "anthropic": "anthropic:claude-haiku-4-5"},
        experiment_id="experiment-1",
    )

    assert result.experiment_id == "experiment-1"
    assert [run.mode for run in result.runs] == ["single", "single", "fanout"]
    assert [run.providers for run in result.runs] == [["gemini"], ["anthropic"], ["gemini", "anthropic"]]
    assert [run.successful_records for run in result.runs] == [2, 2, 4]
    assert {attempt.provider for attempt in result.runs[-1].attempts} == {"gemini", "anthropic"}


async def test_end_to_end_experiment_holds_plan_and_writer_constant(monkeypatch):
    calls = []

    async def fake_run_research(question, **kwargs):
        calls.append((question, kwargs))
        return ResearchReport(
            answer=ResearchAnswer(answer="grounded", claims=[], citations=[], limitations=[]),
        )

    monkeypatch.setattr(experiments, "run_research", fake_run_research)
    plan = ResearchPlan(
        question="fixed question",
        requests=[SearchOrFetchRequest(request_id="s", action="search", query_or_url="fixed query")],
    )
    result = await experiments.run_end_to_end_fixed_plan_experiment(
        plan,
        scenario="fixed-e2e",
        model_map={"gemini": "google:gemini-3.7-flash", "anthropic": "anthropic:claude-haiku-4-5"},
        writer_model="google:gemini-3.7-flash",
        experiment_id="e2e-1",
    )

    assert [run.providers for run in result.runs] == [["gemini"], ["anthropic"], ["gemini", "anthropic"]]
    assert all(kwargs["fixed_plan"].requests == plan.requests for _, kwargs in calls)
    assert all(kwargs["model"] == "google:gemini-3.7-flash" for _, kwargs in calls)
