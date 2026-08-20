"""Gateway Unit Tests for `AntigravitySDKGateway` (PLAN.md Test Plan).

These exercise the hook-based evidence extraction and drift check directly,
without launching a real `localharness` subprocess: `Agent.__aenter__()`
requires the compiled binary and network access, which unit tests must not
depend on. Instead we build a bare gateway instance and drive the
`@pre_tool_call_decide` / `@post_tool_call` hook methods and `_run_turn`
directly with real `google.antigravity.types` objects.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from google.antigravity import LocalAgentConfig, types
from google.antigravity.connections.local import types as local_types
from google.antigravity.hooks import hooks as ga_hooks

from pydagy_research.gateway import AntigravitySDKGateway
from pydagy_research.models import EvidenceRecord


def _gateway() -> AntigravitySDKGateway:
    return AntigravitySDKGateway()


async def test_pre_tool_call_captures_args_and_always_allows():
    gateway = _gateway()
    call = types.ToolCall(name=types.BuiltinTools.SEARCH_WEB.value, args={"query": "python 3.14"}, id="call-1")
    result = await gateway._pre_tool_call(call)
    assert result.allow is True
    assert gateway._pending_call_args["call-1"] == {"query": "python 3.14"}


async def test_post_tool_call_builds_search_summary_record_no_drift():
    gateway = _gateway()
    gateway._expected_action = "search"
    gateway._expected_value = "python 3.14 release date"
    gateway._pending_call_args["call-1"] = {"query": "python 3.14 release date"}

    result = types.ToolResult(
        id="call-1",
        name=types.BuiltinTools.SEARCH_WEB.value,
        result=local_types.SearchWebResult(summary="Python 3.14 was released in October."),
    )
    await gateway._post_tool_call(result)

    assert len(gateway._turn_records) == 1
    record = gateway._turn_records[0]
    assert record.source_kind == "search_summary"
    assert record.status == "success"
    assert record.drift_flagged is False
    assert "October" in record.raw_extract


async def test_post_tool_call_flags_drift_on_mismatched_query():
    gateway = _gateway()
    gateway._expected_action = "search"
    gateway._expected_value = "python 3.14 release date"
    gateway._pending_call_args["call-1"] = {"query": "unrelated topic entirely"}

    result = types.ToolResult(
        id="call-1",
        name=types.BuiltinTools.SEARCH_WEB.value,
        result=local_types.SearchWebResult(summary="some unrelated summary"),
    )
    await gateway._post_tool_call(result)

    assert gateway._turn_records[0].drift_flagged is True


async def test_post_tool_call_flags_drift_on_mismatched_url():
    gateway = _gateway()
    gateway._expected_action = "read"
    gateway._expected_value = "https://example.com/expected"
    gateway._pending_call_args["call-1"] = {"url": "https://example.com/different"}

    result = types.ToolResult(
        id="call-1",
        name=types.BuiltinTools.READ_URL_CONTENT.value,
        result=local_types.ReadUrlContentResult(title="Different Page", summary="text", content_path=""),
    )
    await gateway._post_tool_call(result)

    record = gateway._turn_records[0]
    assert record.source_kind == "page_content"
    assert record.drift_flagged is True


async def test_post_tool_call_builds_page_content_record_no_drift():
    gateway = _gateway()
    gateway._expected_action = "read"
    gateway._expected_value = "https://example.com/expected"
    gateway._pending_call_args["call-1"] = {"url": "https://example.com/expected"}

    result = types.ToolResult(
        id="call-1",
        name=types.BuiltinTools.READ_URL_CONTENT.value,
        result=local_types.ReadUrlContentResult(title="Expected Page", summary="clean text", content_path=""),
    )
    await gateway._post_tool_call(result)

    record = gateway._turn_records[0]
    assert record.source_kind == "page_content"
    assert record.title == "Expected Page"
    assert record.raw_extract == "clean text"
    assert record.drift_flagged is False


async def test_pre_tool_call_lowercases_arg_keys_so_no_false_drift():
    """Regression test: a live session showed read_url_content's wire args

    keyed as {"Url": ...} (capitalized), while search_web's are {"query":
    ...} (lowercase) — the SDK doesn't normalize casing itself. Before the
    fix, _post_tool_call's `args.get("url", "")` silently returned "" for
    every successful read, flagging it as drift and dropping otherwise-good
    evidence at the Validator Node.
    """
    gateway = _gateway()
    call = types.ToolCall(
        name=types.BuiltinTools.READ_URL_CONTENT.value,
        args={"Url": "https://example.com/expected"},  # capitalized, as seen on the wire
        id="call-1",
    )
    await gateway._pre_tool_call(call)

    gateway._expected_action = "read"
    gateway._expected_value = "https://example.com/expected"
    result = types.ToolResult(
        id="call-1",
        name=types.BuiltinTools.READ_URL_CONTENT.value,
        result=local_types.ReadUrlContentResult(title="Expected Page", summary="clean text", content_path=""),
    )
    await gateway._post_tool_call(result)

    record = gateway._turn_records[0]
    assert record.source_url == "https://example.com/expected"
    assert record.drift_flagged is False
    assert record.status == "success"


async def test_post_tool_call_marks_error_result_failed():
    gateway = _gateway()
    gateway._expected_action = "read"
    gateway._expected_value = "https://example.com/expected"
    gateway._pending_call_args["call-1"] = {"url": "https://example.com/expected"}

    result = types.ToolResult(
        id="call-1",
        name=types.BuiltinTools.READ_URL_CONTENT.value,
        result=None,
        error="fetch timed out",
    )
    await gateway._post_tool_call(result)

    record = gateway._turn_records[0]
    assert record.status == "failed"
    assert record.error == "fetch timed out"
    assert record.raw_extract == ""


async def test_post_tool_call_appends_view_file_content_to_last_page_content_record():
    gateway = _gateway()
    gateway._expected_action = "read"
    gateway._expected_value = "https://example.com/big"
    gateway._pending_call_args["call-1"] = {"url": "https://example.com/big"}
    read_result = types.ToolResult(
        id="call-1",
        name=types.BuiltinTools.READ_URL_CONTENT.value,
        result=local_types.ReadUrlContentResult(title="Big Page", summary="short preview", content_path="/tmp/x"),
    )
    await gateway._post_tool_call(read_result)

    view_result = types.ToolResult(
        id="call-2",
        name=types.BuiltinTools.VIEW_FILE.value,
        result=local_types.TextResult(text="the full cached page content"),
    )
    await gateway._post_tool_call(view_result)

    assert len(gateway._turn_records) == 1
    assert "short preview" in gateway._turn_records[0].raw_extract
    assert "the full cached page content" in gateway._turn_records[0].raw_extract


def test_hooks_survive_agent_config_deepcopy():
    """Regression test: `Agent.__init__` does `config.model_copy(deep=True)`

    on the whole `AgentConfig`, including its `hooks` list. Our hooks are
    bound methods of the gateway instance, so `self` (and everything
    reachable from it) must be deepcopy-safe, or construction blows up with
    `TypeError: cannot pickle 'module' object` before a single tool call
    ever happens (see the module-level `_antigravity_types()` docstring in
    gateway.py for why a module reference must never live on `self`).
    """
    gateway = _gateway()
    config = LocalAgentConfig(hooks=gateway._build_hooks(ga_hooks))

    config.model_copy(deep=True)  # must not raise


def test_build_hooks_omits_otel_hooks_by_default():
    gateway = AntigravitySDKGateway()
    all_hooks = gateway._build_hooks(ga_hooks)
    assert len(all_hooks) == 2  # just our pre/post drift-check hooks


def test_build_hooks_adds_sdk_otel_hooks_when_enabled():
    """enable_otel=True registers google.antigravity.utils.otel.get_otel_hooks()

    (session/turn/step/tool-call span hooks) alongside our own, and the
    combined list must still survive Agent.__init__'s config deepcopy --
    same failure class as test_hooks_survive_agent_config_deepcopy above,
    now with 9 additional hook objects mixed in.
    """
    from google.antigravity.utils import otel as otel_hooks

    gateway = AntigravitySDKGateway(enable_otel=True)
    all_hooks = gateway._build_hooks(ga_hooks)

    assert len(all_hooks) == 2 + len(otel_hooks.get_otel_hooks())
    assert any(isinstance(h, otel_hooks.OTelSessionStartHook) for h in all_hooks)
    assert any(isinstance(h, otel_hooks.OTelPostToolCallHook) for h in all_hooks)

    config = LocalAgentConfig(hooks=all_hooks)
    config.model_copy(deep=True)  # must not raise


def _thin_page_content_record(raw_extract: str = "LangChain CVE content") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="RAW-thin",
        source_url="https://cve.mitre.org/cgi-bin/cvekey.cgi?keyword=langchain",
        source_kind="page_content",
        title="MITRE CVE search",
        raw_extract=raw_extract,
        timestamp=datetime.now(timezone.utc),
        status="success",
    )


def test_apply_response_text_fallback_replaces_thin_extract():
    """Regression test for the view_file-doesn't-carry-content finding

    (FINDINGS.md §5): read_url_content/view_file left a 21-character
    non-answer ("LangChain CVE content") in raw_extract; the model's own
    final response text had 2128 characters of real CVE detail. The
    fallback should replace the thin extract with that richer text.
    """
    gateway = _gateway()
    gateway._turn_records = [_thin_page_content_record()]

    rich_text = "CVE-2025-68664: ... " * 10  # well over the 80-char threshold
    gateway._apply_response_text_fallback(rich_text)

    assert gateway._turn_records[0].raw_extract == rich_text.strip()


def test_apply_response_text_fallback_leaves_rich_extract_alone():
    gateway = _gateway()
    rich_original = "A" * 200
    gateway._turn_records = [_thin_page_content_record(rich_original)]

    gateway._apply_response_text_fallback("some other response text " * 10)

    assert gateway._turn_records[0].raw_extract == rich_original


def test_apply_response_text_fallback_does_nothing_when_response_text_also_thin():
    gateway = _gateway()
    original = "LangChain CVE content"
    gateway._turn_records = [_thin_page_content_record(original)]

    gateway._apply_response_text_fallback("also thin")

    assert gateway._turn_records[0].raw_extract == original


def test_apply_response_text_fallback_ignores_failed_and_search_summary_records():
    gateway = _gateway()
    failed = _thin_page_content_record("").model_copy(update={"status": "failed"})
    search_summary = _thin_page_content_record().model_copy(update={"source_kind": "search_summary"})
    gateway._turn_records = [failed, search_summary]

    gateway._apply_response_text_fallback("rich response text " * 10)

    assert gateway._turn_records[0].raw_extract == ""
    assert gateway._turn_records[1].raw_extract == "LangChain CVE content"


class _RaisingAgent:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def chat(self, prompt: str):
        raise self._exc


async def test_run_turn_translates_sdk_exception_to_failed_record():
    gateway = _gateway()
    gateway._agent = _RaisingAgent(types.AntigravityExecutionError("harness crashed"))

    records = await gateway._run_turn(action="search", value="python 3.14", prompt="irrelevant")

    assert len(records) == 1
    assert records[0].status == "failed"
    assert "harness crashed" in records[0].error


class _SilentAgent:
    """Simulates a turn where the model never invoked the expected tool."""

    async def chat(self, prompt: str):
        class _Resp:
            async def text(self) -> str:
                return "I could not find that."

        return _Resp()


async def test_search_returns_missing_call_record_when_tool_never_invoked():
    gateway = _gateway()
    gateway._agent = _SilentAgent()

    records = await gateway.search("python 3.14 release date")

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].drift_flagged is True
