"""Tests for MultiProviderGateway (MULTI-PROVIDER-PLAN.md §3)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pydagy_research.models import EvidenceRecord
from pydagy_research.multi_provider_gateway import MultiProviderGateway


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeGateway:
    """Fake gateway returning canned results."""

    def __init__(self, records: list[EvidenceRecord]):
        self._records = records

    async def __aenter__(self) -> "_FakeGateway":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
        return list(self._records)

    async def read(self, url: str) -> list[EvidenceRecord]:
        return list(self._records)


@pytest.fixture
def gemini_record():
    return EvidenceRecord(
        evidence_id="RAW-gemini",
        source_url="https://gemini-result.com",
        source_kind="page_content",
        title="Gemini Result",
        raw_extract="Gemini found this content",
        timestamp=_now(),
        status="success",
    )


@pytest.fixture
def anthropic_record():
    return EvidenceRecord(
        evidence_id="RAW-anthropic",
        source_url="https://anthropic-result.com",
        source_kind="page_content",
        title="Anthropic Result",
        raw_extract="Anthropic found this content",
        timestamp=_now(),
        status="success",
    )


@pytest.mark.asyncio
async def test_search_fans_out_to_all_providers(gemini_record, anthropic_record):
    """search() should call all providers concurrently and return all results."""
    gateways = {
        "gemini": _FakeGateway([gemini_record]),
        "anthropic": _FakeGateway([anthropic_record]),
    }
    multi = MultiProviderGateway(gateways)

    async with multi:
        records = await multi.search("test query")

    assert len(records) == 2
    # Both records should be tagged with their provider
    assert any(r.provider == "gemini" for r in records)
    assert any(r.provider == "anthropic" for r in records)


@pytest.mark.asyncio
async def test_read_fans_out_to_read_capable_providers(gemini_record, anthropic_record):
    """read() should only call read-capable providers."""
    gateways = {
        "gemini": _FakeGateway([gemini_record]),
        "anthropic": _FakeGateway([anthropic_record]),
        "openai": _FakeGateway([]),  # search-only
    }
    # Mark openai as not read-capable
    read_capable = {"gemini", "anthropic"}
    multi = MultiProviderGateway(gateways, read_capable=read_capable)

    async with multi:
        records = await multi.read("https://example.com")

    # Should only get results from gemini and anthropic, not openai
    assert len(records) == 2
    providers = {r.provider for r in records}
    assert providers == {"gemini", "anthropic"}


@pytest.mark.asyncio
async def test_returns_all_successful_records_not_single_best(gemini_record, anthropic_record):
    """Should return ALL successful records, not pick a "best" one."""
    # Same URL, different providers → should keep both (corroboration)
    same_url_gemini = EvidenceRecord(
        evidence_id="RAW-g1",
        source_url="https://example.com",
        source_kind="page_content",
        title="Example",
        raw_extract="Gemini's view of example.com",
        timestamp=_now(),
        status="success",
    )
    same_url_anthropic = EvidenceRecord(
        evidence_id="RAW-a1",
        source_url="https://example.com",
        source_kind="page_content",
        title="Example",
        raw_extract="Anthropic's view of example.com",
        timestamp=_now(),
        status="success",
    )

    gateways = {
        "gemini": _FakeGateway([same_url_gemini]),
        "anthropic": _FakeGateway([same_url_anthropic]),
    }
    multi = MultiProviderGateway(gateways)

    async with multi:
        records = await multi.read("https://example.com")

    # Both should be returned, tagged with provider
    assert len(records) == 2
    assert records[0].provider in {"gemini", "anthropic"}
    assert records[1].provider in {"gemini", "anthropic"}
    assert records[0].provider != records[1].provider  # Different providers


@pytest.mark.asyncio
async def test_tags_all_results_with_provider():
    """Every returned record should have provider field set."""
    record1 = EvidenceRecord(
        evidence_id="RAW-1",
        source_url="https://example.com",
        source_kind="page_content",
        title="Example",
        raw_extract="content",
        timestamp=_now(),
        status="success",
    )
    record2 = EvidenceRecord(
        evidence_id="RAW-2",
        source_url="https://other.com",
        source_kind="page_content",
        title="Other",
        raw_extract="content",
        timestamp=_now(),
        status="success",
    )

    gateways = {
        "provider1": _FakeGateway([record1]),
        "provider2": _FakeGateway([record2]),
    }
    multi = MultiProviderGateway(gateways)

    async with multi:
        records = await multi.search("query")

    for record in records:
        assert record.provider is not None
        assert record.provider in {"provider1", "provider2"}


@pytest.mark.asyncio
async def test_handles_provider_failure_gracefully():
    """If one provider fails, should continue with others."""

    class _FailingGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            pass

        async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
            raise RuntimeError("Provider crashed")

        async def read(self, url: str) -> list[EvidenceRecord]:
            raise RuntimeError("Provider crashed")

    success_record = EvidenceRecord(
        evidence_id="RAW-ok",
        source_url="https://ok.com",
        source_kind="page_content",
        title="OK",
        raw_extract="content",
        timestamp=_now(),
        status="success",
    )

    gateways = {
        "failing": _FailingGateway(),
        "ok": _FakeGateway([success_record]),
    }
    multi = MultiProviderGateway(gateways)

    async with multi:
        records = await multi.search("query")

    # Should return result from OK provider, skip failing one
    assert len(records) == 1
    assert records[0].provider == "ok"


@pytest.mark.asyncio
async def test_search_with_domain_restriction():
    """search() should pass domain restriction to all providers."""
    record = EvidenceRecord(
        evidence_id="RAW-1",
        source_url="https://example.com/page",
        source_kind="search_summary",
        title="Example search",
        raw_extract="Found on example.com",
        timestamp=_now(),
        status="success",
    )

    # Track calls to verify domain was passed
    called_with = []

    class _TrackingGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            pass

        async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
            called_with.append((query, domain))
            return [record]

        async def read(self, url: str) -> list[EvidenceRecord]:
            return []

    gateways = {
        "p1": _TrackingGateway(),
        "p2": _TrackingGateway(),
    }
    multi = MultiProviderGateway(gateways)

    async with multi:
        await multi.search("test query", domain="example.com")

    # Both providers should have been called with domain
    assert len(called_with) == 2
    for query, domain in called_with:
        assert query == "test query"
        assert domain == "example.com"
