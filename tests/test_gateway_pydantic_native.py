"""Gateway Unit Tests for `PydanticNativeSearchGateway` (PLAN.md Test Plan:
"Run the same suite (mocked) against PydanticNativeSearchGateway to confirm
both backends satisfy the identical EvidenceRecord/source_kind contract.").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from pydagy_research.gateway import PydanticNativeSearchGateway, _extract_native_evidence


def _bare_gateway() -> PydanticNativeSearchGateway:
    # Bypass __init__ (which constructs real pydantic_ai Agents) so tests can
    # swap in fake search/fetch agents without touching a real model.
    return object.__new__(PydanticNativeSearchGateway)


@dataclass
class _FakePart:
    part_kind: str
    content: object = None


@dataclass
class _FakeMessage:
    parts: list[_FakePart] = field(default_factory=list)


@dataclass
class _FakeResult:
    output: str
    messages: list[_FakeMessage] = field(default_factory=list)

    def new_messages(self) -> list[_FakeMessage]:
        return self.messages


def test_extract_native_evidence_itemizes_builtin_tool_return_parts():
    result = _FakeResult(
        output="ignored when structured sources are present",
        messages=[
            _FakeMessage(
                parts=[
                    _FakePart(
                        part_kind="builtin-tool-return",
                        content={"url": "https://a.example/1", "title": "Source A", "snippet": "a snippet"},
                    ),
                    _FakePart(
                        part_kind="builtin-tool-return",
                        content={"url": "https://b.example/2", "title": "Source B", "snippet": "b snippet"},
                    ),
                    _FakePart(part_kind="text", content="model prose, not a tool return"),
                ]
            )
        ],
    )

    records = _extract_native_evidence(
        result, source_kind="page_content", fallback_url="https://fallback", fallback_title="fallback"
    )

    assert len(records) == 2
    assert {r.source_url for r in records} == {"https://a.example/1", "https://b.example/2"}
    assert all(r.source_kind == "page_content" for r in records)
    assert all(r.status == "success" for r in records)


def test_extract_native_evidence_falls_back_to_opaque_output_text():
    result = _FakeResult(output="a plain synthesized answer with no itemized sources", messages=[])

    records = _extract_native_evidence(
        result, source_kind="search_summary", fallback_url="search:q", fallback_title="Search: q"
    )

    assert len(records) == 1
    assert records[0].source_kind == "search_summary"
    assert records[0].raw_extract == "a plain synthesized answer with no itemized sources"
    assert records[0].source_url == "search:q"


def test_extract_native_evidence_returns_empty_list_for_empty_output():
    result = _FakeResult(output="", messages=[])
    assert _extract_native_evidence(result, source_kind="page_content", fallback_url="u", fallback_title="t") == []


class _RaisingAgent:
    async def run(self, prompt: str):
        raise UnexpectedModelBehavior("model refused to call the tool")


async def test_search_translates_agent_run_error_to_failed_record():
    gateway = _bare_gateway()
    gateway._search_agent = _RaisingAgent()

    records = await gateway.search("python 3.14 release date")

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].source_kind == "search_summary"
    assert "model refused" in records[0].error


async def test_read_translates_agent_run_error_to_failed_record():
    gateway = _bare_gateway()
    gateway._fetch_agent = _RaisingAgent()

    record = await gateway.read("https://example.com/page")

    assert record.status == "failed"
    assert record.source_kind == "page_content"
    assert record.source_url == "https://example.com/page"


class _EmptyAgent:
    async def run(self, prompt: str):
        return SimpleNamespace(output="", new_messages=lambda: [])


async def test_read_returns_failed_record_when_no_content_returned():
    gateway = _bare_gateway()
    gateway._fetch_agent = _EmptyAgent()

    record = await gateway.read("https://example.com/empty")

    assert record.status == "failed"
    assert record.error == "no content returned"
