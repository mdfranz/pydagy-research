"""Gateway Unit Tests for `AntigravitySDKGateway` (PLAN.md Test Plan).

These exercise the hook-based evidence extraction and drift check directly,
without launching a real `localharness` subprocess: `Agent.__aenter__()`
requires the compiled binary and network access, which unit tests must not
depend on. Instead we build a bare gateway instance and drive the
`@pre_tool_call_decide` / `@post_tool_call` hook methods and `_run_turn`
directly with real `google.antigravity.types` objects.
"""

from __future__ import annotations

import pytest
from google.antigravity import LocalAgentConfig, types
from google.antigravity.connections.local import types as local_types
from google.antigravity.hooks import hooks as ga_hooks

from pydagy_research.gateway import AntigravitySDKGateway


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
    pre_hook = ga_hooks.pre_tool_call_decide(gateway._pre_tool_call)
    post_hook = ga_hooks.post_tool_call(gateway._post_tool_call)
    config = LocalAgentConfig(hooks=[pre_hook, post_hook])

    config.model_copy(deep=True)  # must not raise


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
