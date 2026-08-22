"""Retrieval Gateway abstraction and backend implementations (PLAN.md §1).

Two interchangeable backends satisfy the same `RetrievalGateway` protocol and
the same `EvidenceRecord` contract (PLAN.md §5):

* `AntigravitySDKGateway` (default) — wraps a narrowly-capability-scoped
  `google.antigravity.Agent` session (PLAN.md §1.1).
* `PydanticNativeSearchGateway` — a plain `pydantic_ai.Agent` configured with
  `WebSearch()` / `WebFetch()` capabilities. No `localharness` subprocess.

`google-antigravity` is an optional dependency (see `pyproject.toml`'s
`antigravity` extra) — it is only imported inside `AntigravitySDKGateway`,
so importing this module never requires it to be installed.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from .models import EvidenceRecord, ResearchPlan, SearchOrFetchRequest

if TYPE_CHECKING:
    from .telemetry import TelemetryRecorder

__all__ = [
    "RetrievalGateway",
    "AntigravitySDKGateway",
    "PydanticNativeSearchGateway",
    "make_gateway",
]

_logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _temp_evidence_id() -> str:
    # Placeholder id; the Evidence Validator Node (PLAN.md §6.3) assigns the
    # stable EVID-xxx id once records are deduplicated and pooled.
    return f"RAW-{uuid.uuid4().hex[:10]}"


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


# Below this many characters, a read_url_content/view_file extraction is
# treated as "too thin to be useful" and AntigravitySDKGateway.read() falls
# back to the model's own response text instead (see
# _apply_response_text_fallback's docstring for why, and FINDINGS.md §5 for
# the live trace that led to this). A judgment call, not a precise
# threshold: real single-fact extracts (e.g. a page <title>) tend to land
# well above this; the failure mode this guards against was a one-line
# non-answer caption in the low tens of characters.
_MIN_USEFUL_READ_CHARS = 80

_antigravity_types_module: Any = None


def _antigravity_types() -> Any:
    """Lazily imports and memoizes `google.antigravity.types` at module scope.

    Deliberately NOT cached on a gateway instance attribute: `Agent.__init__`
    deep-copies the whole `AgentConfig` it's given (including the `hooks`
    list), and our hooks are bound methods of the gateway — so anything
    reachable from `self` must stay deepcopy-safe. A live module object is
    not (`TypeError: cannot pickle 'module' object`); a module-level global
    sidesteps that entirely.
    """
    global _antigravity_types_module
    if _antigravity_types_module is None:
        from google.antigravity import types as _types

        _antigravity_types_module = _types
    return _antigravity_types_module


class RetrievalGateway(Protocol):
    """Backend-agnostic retrieval interface (PLAN.md §1).

    Both search() and read() return lists to support multi-provider fan-out
    (MULTI-PROVIDER-PLAN.md §3-4): a single logical request may produce
    multiple independent records from different backends.
    """

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]: ...

    async def read(self, url: str) -> list[EvidenceRecord]: ...


async def run_plan(gateway: RetrievalGateway, plan: ResearchPlan) -> list[EvidenceRecord]:
    """Executes a `ResearchPlan`'s requests sequentially against `gateway`.

    Requests execute one at a time (PLAN.md §3: one Antigravity session is
    turn-based, not concurrent) regardless of which backend is configured, so
    this helper is backend-agnostic and lives outside both gateway classes.

    Both search() and read() return lists (MULTI-PROVIDER-PLAN.md §3-4):
    read() may return multiple records from different providers on fan-out,
    or a singleton list on single-provider backends.
    """
    records: list[EvidenceRecord] = []
    for request in plan.requests:
        if request.action == "search":
            records.extend(await gateway.search(request.query_or_url, request.domain))
        else:
            records.extend(await gateway.read(request.query_or_url))
    return records


# ---------------------------------------------------------------------------
# Backend A: Antigravity SDK Gateway (default)
# ---------------------------------------------------------------------------


class AntigravitySDKGateway:
    """Sandboxed Antigravity SDK worker used purely as a retrieval gateway.

    Capability allowlisting, fail-closed policies, and budget sizing follow
    PLAN.md §1.1 exactly. Retrieval is always LLM-mediated (PLAN.md "Known
    Constraints") — there is no RPC to force `search_web(query=X)` — so every
    `search()`/`read()` call is a single `agent.chat()` turn whose actually
    -executed tool args are compared against what was requested via a
    `@post_tool_call` drift check (PLAN.md §2, §6.2).

    Must be used as an async context manager so one `localharness` session is
    reused across an entire `ResearchPlan` (PLAN.md §3):

        async with AntigravitySDKGateway() as gateway:
            records = await run_plan(gateway, plan)

    `enable_otel=True` additionally registers the SDK's own
    `google.antigravity.utils.otel.get_otel_hooks()` — standard
    OpenTelemetry spans for the session/turn/tool-call lifecycle inside
    `localharness`, which `logfire.instrument_pydantic_ai()` (tracing.py)
    cannot see since this backend never uses a `pydantic_ai.Agent`. Once
    `logfire.configure()` has run, these spans land in the same trace
    automatically — they're emitted via the global OTel tracer, and
    `logfire.configure()` registers itself as that global provider — no
    separate exporter wiring needed.
    """

    def __init__(
        self,
        *,
        model: Any | None = None,
        budget_config: Any | None = None,
        agent_config_kwargs: dict[str, Any] | None = None,
        enable_otel: bool = False,
        telemetry_recorder: "TelemetryRecorder | None" = None,
    ) -> None:
        self._model = model
        self._budget_config = budget_config
        self._agent_config_kwargs = dict(agent_config_kwargs or {})
        self._enable_otel = enable_otel
        self._telemetry_recorder = telemetry_recorder
        self._agent: Any | None = None
        self._agent_cm: Any | None = None
        # id (ToolCall.id) -> args, captured by the pre-tool-call hook so the
        # post-tool-call hook can see what was *actually* executed.
        self._pending_call_args: dict[str, dict[str, Any]] = {}
        self._expected_action: str | None = None
        self._expected_value: str | None = None
        self._turn_records: list[EvidenceRecord] = []
        self._turn_start_time: float = 0.0

    async def __aenter__(self) -> "AntigravitySDKGateway":
        # Imported lazily: google-antigravity is an optional extra.
        from google.antigravity import Agent, LocalAgentConfig, types
        from google.antigravity.hooks import hooks, policy

        budget_config = self._budget_config or types.BudgetConfig(
            max_tool_calls=12, max_model_calls=10
        )
        capabilities = types.CapabilitiesConfig(
            enabled_tools=[
                types.BuiltinTools.SEARCH_WEB,
                types.BuiltinTools.READ_URL_CONTENT,
                types.BuiltinTools.VIEW_FILE,
            ]
        )
        policies = [
            policy.deny_all(),
            policy.allow(types.BuiltinTools.SEARCH_WEB.value),
            policy.allow(types.BuiltinTools.READ_URL_CONTENT.value),
            policy.allow(types.BuiltinTools.VIEW_FILE.value),
        ]

        all_hooks = self._build_hooks(hooks)

        config_kwargs: dict[str, Any] = dict(self._agent_config_kwargs)
        if self._model is not None:
            config_kwargs.setdefault("model", self._model)
        config = LocalAgentConfig(
            capabilities=capabilities,
            policies=policies,
            hooks=all_hooks,
            budget_config=budget_config,
            **config_kwargs,
        )
        self._agent = Agent(config)
        await self._agent.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._agent is not None:
            await self._agent.__aexit__(*exc_info)
            self._agent = None

    # -- RetrievalGateway protocol -----------------------------------------

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
        prompt = f"Use the search_web tool to search the web for exactly: {query!r}."
        if domain:
            prompt += f" Restrict the search to domain: {domain!r}."
        records = await self._run_turn(action="search", value=query, prompt=prompt)
        return records or [self._missing_call_record(action="search", value=query)]

    async def read(self, url: str) -> list[EvidenceRecord]:
        prompt = (
            f"Use the read_url_content tool to read exactly this URL: {url!r}. "
            "If the tool's summary comes back empty or just a few words, that "
            "almost always means the page was too large to summarize and got "
            "cached to disk instead — read_url_content will have populated "
            "content_path in that case (see its content_path field). You MUST "
            "then call view_file on that content_path before finishing this "
            "turn; a thin or empty summary is not a completed read, it's a "
            "signal to go look at the cached file."
        )
        records = await self._run_turn(action="read", value=url, prompt=prompt)
        for record in records:
            if record.source_kind == "page_content":
                return [record]
        return [self._missing_call_record(action="read", value=url)]

    # -- internals -----------------------------------------------------------

    def _build_hooks(self, hooks_module: Any) -> list[Any]:
        """Builds the hooks list passed to `LocalAgentConfig` (split out for testability).

        `hooks_module` is `google.antigravity.hooks.hooks`, passed in from
        `__aenter__` (it's imported lazily there since the SDK is an
        optional dependency).
        """
        pre_hook = hooks_module.pre_tool_call_decide(self._pre_tool_call)
        post_hook = hooks_module.post_tool_call(self._post_tool_call)
        all_hooks = [pre_hook, post_hook]
        if self._enable_otel:
            from google.antigravity.utils import otel as otel_hooks

            # Independent hook sets on the same hook types (pre_tool_call_decide,
            # post_tool_call, ...): HookRunner dispatches every registered hook
            # of a given type each time, so the SDK's span-management hooks and
            # our own evidence-extraction hooks coexist without interfering
            # with each other (verified against hook_runner.py's dispatch_*
            # methods, which just loop over the registered list).
            all_hooks = all_hooks + otel_hooks.get_otel_hooks()
        return all_hooks

    async def _run_turn(self, *, action: str, value: str, prompt: str) -> list[EvidenceRecord]:
        assert self._agent is not None, "AntigravitySDKGateway must be used as `async with gateway:`"
        types = _antigravity_types()
        self._expected_action = action
        self._expected_value = value
        self._turn_records = []
        self._turn_start_time = time.time()
        self._pending_call_args.clear()  # defensive: no carry-over if a prior turn errored mid-call
        try:
            response = await self._agent.chat(prompt)
            response_text = await response.text()
        except (
            types.AntigravityConnectionError,
            types.AntigravityExecutionError,
            types.ToolExecutionError,
            types.AntigravityValidationError,
        ) as exc:
            _logger.warning("Antigravity gateway turn failed (%s=%r): %s", action, value, exc)
            self._record_attempt_telemetry(action, value, status="failed", error=str(exc))
            return [self._failed_record(action=action, value=value, error=str(exc))]

        if action == "read":
            self._apply_response_text_fallback(response_text)

        # Record telemetry for each record produced
        for record in self._turn_records:
            self._record_attempt_telemetry(
                action,
                value,
                status=record.status,
                char_count=len(record.raw_extract),
                drift_flagged=record.drift_flagged,
                error=record.error,
            )
        return list(self._turn_records)

    def _record_attempt_telemetry(
        self,
        action: str,
        value: str,
        *,
        status: str,
        char_count: int = 0,
        drift_flagged: bool = False,
        error: str | None = None,
    ) -> None:
        """Record a retrieval attempt to telemetry if recorder is configured."""
        if not hasattr(self, "_telemetry_recorder") or self._telemetry_recorder is None:
            return

        from .telemetry import SourceAttempt

        duration_ms = (time.time() - self._turn_start_time) * 1000
        tier = "excluded" if status != "success" else ("thin" if char_count < 80 else "verified")
        extraction_method = "antigravity_tool"

        attempt = SourceAttempt(
            request_id=f"ag-{uuid.uuid4().hex[:8]}",
            provider="antigravity",
            action=action,
            query_or_url=value,
            tier=tier,
            status=status,
            char_count=char_count,
            duration_ms=duration_ms,
            extraction_method=extraction_method,
            model=str(self._model) if self._model else None,
            drift_flagged=drift_flagged,
            error=error,
        )
        self._telemetry_recorder.record_attempt(attempt)

    def _apply_response_text_fallback(self, response_text: str) -> None:
        """Falls back to the model's own final response text when typed extraction came back too thin.

        Confirmed live (FINDINGS.md §5): `view_file`'s `PostTool` payload is
        NOT the viewed file's content. `VIEW_FILE` isn't in the SDK's
        `_TOOL_RESULT_MODELS` mapping (only `RUN_COMMAND`/`LIST_DIR`/
        `FIND_FILE`/`SEARCH_DIR`/`EDIT_FILE`/`GENERATE_IMAGE`/`SEARCH_WEB`/
        `READ_URL_CONTENT` are), so the post-tool-call hook only ever sees a
        short caption like `"View cached LangChain CVE results"` — never the
        file's actual text. On a large/cached page, the real content the
        model saw is only recoverable from its own final response text,
        which is exactly the "ungrounded model paraphrase" PLAN.md §2 says
        must never become `raw_extract` — so this is a deliberate, narrow
        exception, used only when the typed extraction is empty or
        near-empty (confirmed live: without it, `read_url_content` ->
        empty summary -> `view_file` -> a caption, not content -> the
        pipeline had nothing at all for large pages). This mirrors the same
        accepted trade-off `PydanticNativeSearchGateway` already makes for
        its own backend (PLAN.md §1: "falling back to raw_extract as an
        opaque blob otherwise").
        """
        response_text = response_text.strip()
        if not response_text:
            return
        for record in self._turn_records:
            if record.source_kind != "page_content" or record.status != "success":
                continue
            if len(record.raw_extract.strip()) < _MIN_USEFUL_READ_CHARS < len(response_text):
                _logger.info(
                    "read_url_content/view_file extraction was thin (%d chars) for %s;"
                    " falling back to the model's own response text (%d chars)",
                    len(record.raw_extract),
                    record.source_url,
                    len(response_text),
                )
                record.raw_extract = response_text

    def _missing_call_record(self, *, action: str, value: str) -> EvidenceRecord:
        return self._failed_record(
            action=action,
            value=value,
            error="model did not invoke the expected tool for this request",
            drift_flagged=True,
        )

    def _failed_record(
        self, *, action: str, value: str, error: str, drift_flagged: bool = False
    ) -> EvidenceRecord:
        source_kind = "page_content" if action == "read" else "search_summary"
        source_url = value if action == "read" else f"search:{value}"
        return EvidenceRecord(
            evidence_id=_temp_evidence_id(),
            source_url=source_url,
            source_kind=source_kind,
            title=value,
            raw_extract="",
            timestamp=_now(),
            status="failed",
            drift_flagged=drift_flagged,
            error=error,
        )

    async def _pre_tool_call(self, data: Any) -> Any:
        types = _antigravity_types()
        if data.id and data.name in (
            types.BuiltinTools.SEARCH_WEB.value,
            types.BuiltinTools.READ_URL_CONTENT.value,
        ):
            # Wire arg key casing is tool-specific and not normalized by the
            # SDK (search_web sends "query", read_url_content sends "Url" —
            # confirmed against a live session's raw hook payloads). Lowercase
            # keys here so _post_tool_call's args.get("query"/"url") lookups
            # aren't silently empty, which would falsely flag every
            # successful read as drift.
            self._pending_call_args[data.id] = {k.lower(): v for k, v in data.args.items()}
        return types.HookResult(allow=True)

    async def _post_tool_call(self, data: Any) -> None:
        """Extracts `EvidenceRecord`s from tool results (PLAN.md §2) and flags drift."""
        types = _antigravity_types()
        from google.antigravity.connections.local import types as local_types

        args = self._pending_call_args.pop(data.id, {}) if data.id else {}

        if data.name == types.BuiltinTools.SEARCH_WEB.value:
            executed_query = args.get("query", "")
            drift = self._expected_action != "search" or _normalize(executed_query) != _normalize(
                self._expected_value or ""
            )
            payload = data.result if isinstance(data.result, local_types.SearchWebResult) else None
            self._turn_records.append(
                EvidenceRecord(
                    evidence_id=_temp_evidence_id(),
                    source_url=f"search:{executed_query or self._expected_value or ''}",
                    source_kind="search_summary",
                    title=f"Search: {executed_query or self._expected_value or ''}",
                    raw_extract="" if data.error else (payload.summary if payload else ""),
                    timestamp=_now(),
                    status="failed" if data.error else "success",
                    drift_flagged=drift,
                    error=data.error,
                )
            )
        elif data.name == types.BuiltinTools.READ_URL_CONTENT.value:
            executed_url = args.get("url", "")
            drift = self._expected_action != "read" or _normalize(executed_url) != _normalize(
                self._expected_value or ""
            )
            payload = data.result if isinstance(data.result, local_types.ReadUrlContentResult) else None
            self._turn_records.append(
                EvidenceRecord(
                    evidence_id=_temp_evidence_id(),
                    source_url=executed_url or self._expected_value or "",
                    source_kind="page_content",
                    title=(payload.title if payload else "") or executed_url,
                    raw_extract="" if data.error else (payload.summary if payload else ""),
                    timestamp=_now(),
                    status="failed" if data.error else "success",
                    drift_flagged=drift,
                    error=data.error,
                )
            )
        elif data.name == types.BuiltinTools.VIEW_FILE.value and not data.error:
            # Large read_url_content pages spill to content_path; VIEW_FILE lets
            # the model inspect the cached file (PLAN.md §1.1). Fold that text
            # into the page_content record it followed, if any.
            text = getattr(data.result, "text", None) or (
                data.result if isinstance(data.result, str) else None
            )
            if text and self._turn_records:
                last = self._turn_records[-1]
                if last.source_kind == "page_content" and last.status == "success":
                    last.raw_extract = f"{last.raw_extract}\n\n{text}".strip()


# ---------------------------------------------------------------------------
# Backend B: Pydantic AI Native Web Search Gateway
# ---------------------------------------------------------------------------


class PydanticNativeSearchGateway:
    """Retrieval gateway built on `pydantic_ai`'s native `WebSearch`/`WebFetch`.

    No `localharness` subprocess and no `google-antigravity` dependency.
    Per-source URL/title/snippet is extracted from the model's native
    tool-call grounding metadata (`NativeToolReturnPart`) where the provider
    supplies it; otherwise the whole response text becomes one opaque
    `raw_extract` record (PLAN.md §1).
    """

    def __init__(
        self,
        model: Any,
        *,
        search_kwargs: dict[str, Any] | None = None,
        fetch_kwargs: dict[str, Any] | None = None,
        telemetry_recorder: "TelemetryRecorder | None" = None,
        telemetry_provider: str = "pydantic_native",
    ) -> None:
        from pydantic_ai import Agent as PydanticAgent
        from pydantic_ai.capabilities import WebFetch, WebSearch

        self._model = model
        self._telemetry_recorder = telemetry_recorder
        self._telemetry_provider = telemetry_provider
        self._search_agent: Any = PydanticAgent(
            model, name="native_search_agent", capabilities=[WebSearch(**(search_kwargs or {}))]
        )
        self._fetch_agent: Any = PydanticAgent(
            model, name="native_fetch_agent", capabilities=[WebFetch(**(fetch_kwargs or {}))]
        )

    async def __aenter__(self) -> "PydanticNativeSearchGateway":
        # No subprocess/session to open (PLAN.md §1) — kept for interface
        # parity with AntigravitySDKGateway so callers can always write
        # `async with make_gateway(plan) as gateway:`.
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
        prompt = query if not domain else f"{query} (restrict results to domain: {domain})"
        records = await self._run(
            self._search_agent,
            prompt,
            source_kind="search_summary",
            fallback_url=f"search:{query}",
            fallback_title=f"Search: {query}",
            action="search",
        )
        return records

    async def read(self, url: str) -> list[EvidenceRecord]:
        records = await self._run(
            self._fetch_agent,
            f"Fetch and return the full contents of {url}",
            source_kind="page_content",
            fallback_url=url,
            fallback_title=url,
            action="read",
        )
        if records:
            return records
        # No records returned - record this as a failure
        self._record_attempt_telemetry("read", url, status="failed", error="no content returned")
        return [
            EvidenceRecord(
                evidence_id=_temp_evidence_id(),
                source_url=url,
                source_kind="page_content",
                title=url,
                raw_extract="",
                timestamp=_now(),
                status="failed",
                error="no content returned",
            )
        ]

    async def _run(
        self,
        agent: Any,
        prompt: str,
        *,
        source_kind: str,
        fallback_url: str,
        fallback_title: str,
        action: str,
    ) -> list[EvidenceRecord]:
        from pydantic_ai.exceptions import AgentRunError

        start_time = time.time()
        try:
            result = await agent.run(prompt)
        except AgentRunError as exc:
            _logger.warning("Native search/fetch turn failed (%s): %s", fallback_url, exc)
            self._record_attempt_telemetry(
                action, fallback_url, status="failed", error=str(exc), duration_ms=(time.time() - start_time) * 1000
            )
            return [
                EvidenceRecord(
                    evidence_id=_temp_evidence_id(),
                    source_url=fallback_url,
                    source_kind=source_kind,
                    title=fallback_title,
                    raw_extract="",
                    timestamp=_now(),
                    status="failed",
                    error=str(exc),
                )
            ]

        records = _extract_native_evidence(
            result, source_kind=source_kind, fallback_url=fallback_url, fallback_title=fallback_title
        )

        # Record telemetry for each extracted record
        for record in records:
            tier = "excluded" if record.status != "success" else ("thin" if len(record.raw_extract) < 80 else "verified")
            self._record_attempt_telemetry(
                action,
                fallback_url,
                status=record.status,
                char_count=len(record.raw_extract),
                tier=tier,
                duration_ms=(time.time() - start_time) * 1000,
            )

        return records

    def _record_attempt_telemetry(
        self,
        action: str,
        query_or_url: str,
        *,
        status: str,
        char_count: int = 0,
        tier: str = "excluded",
        duration_ms: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Record a retrieval attempt to telemetry if recorder is configured."""
        if not hasattr(self, "_telemetry_recorder") or self._telemetry_recorder is None:
            return

        from .telemetry import SourceAttempt

        attempt = SourceAttempt(
            request_id=f"pn-{uuid.uuid4().hex[:8]}",
            provider=getattr(self, "_telemetry_provider", "pydantic_native"),
            action=action,
            query_or_url=query_or_url,
            tier=tier,
            status=status,
            char_count=char_count,
            duration_ms=duration_ms,
            extraction_method="native_tool",
            model=str(self._model) if self._model else None,
            error=error,
        )
        self._telemetry_recorder.record_attempt(attempt)



# Field names that indicate a `builtin-tool-return` part's `content` dict
# actually carries fetched/searched text, as opposed to pure retrieval
# bookkeeping. Confirmed necessary against a live call: Google's `web_fetch`
# tool-return content is `{"url_metadata": [{"retrieved_url": ..., "
# url_retrieval_status": "URL_RETRIEVAL_STATUS_SUCCESS"}]}` -- status
# metadata only, no page text anywhere in it. Treating that dict's `str()`
# as `raw_extract` (the original bug here) produced citations whose
# "snippet" was literally that status dict's repr instead of real content.
# The actual fetched text only shows up in the model's own subsequent
# TextPart, which is exactly the "ungrounded model prose" PLAN.md §2 says
# must never become raw_extract -- so when a provider's tool-return has no
# real content field, the right move is to fall through to the
# result.output opaque-blob fallback below, not to fabricate a record from
# metadata that was never meant to be evidence.
_CONTENT_TEXT_KEYS = ("snippet", "text", "content", "extract", "body", "summary")


def _extract_native_evidence(
    result: Any, *, source_kind: str, fallback_url: str, fallback_title: str
) -> list[EvidenceRecord]:
    """Best-effort per-source extraction from native tool-call grounding metadata.

    Falls back to one opaque `raw_extract` record built from `result.output`
    when the provider doesn't itemize sources, or itemizes only retrieval
    status with no actual content (PLAN.md §1).
    """
    records: list[EvidenceRecord] = []
    try:
        messages = result.new_messages()
    except Exception:  # pragma: no cover - defensive; result shape varies by provider
        messages = []

    for message in messages:
        for part in getattr(message, "parts", []):
            if getattr(part, "part_kind", None) != "builtin-tool-return":
                continue
            content = getattr(part, "content", None)
            if content is None:
                continue
            title = fallback_title
            url = fallback_url
            if isinstance(content, dict):
                if not any(key in content for key in _CONTENT_TEXT_KEYS):
                    continue  # status/metadata only -- fall through to the output-text fallback
                url = content.get("url") or content.get("source_url") or url
                title = content.get("title") or title
            records.append(
                EvidenceRecord(
                    evidence_id=_temp_evidence_id(),
                    source_url=url,
                    source_kind=source_kind,
                    title=title,
                    raw_extract=str(content),
                    timestamp=_now(),
                    status="success",
                )
            )

    if records:
        return records

    output_text = str(getattr(result, "output", "") or "")
    if not output_text:
        return []
    return [
        EvidenceRecord(
            evidence_id=_temp_evidence_id(),
            source_url=fallback_url,
            source_kind=source_kind,
            title=fallback_title,
            raw_extract=output_text,
            timestamp=_now(),
            status="success",
        )
    ]


def make_gateway(
    plan: ResearchPlan, telemetry_recorder: "TelemetryRecorder | None" = None, **kwargs: Any
) -> RetrievalGateway:
    """Instantiates the `RetrievalGateway` selected by `plan.retrieval_backend` (PLAN.md §1)."""
    if plan.retrieval_backend == "antigravity":
        return AntigravitySDKGateway(telemetry_recorder=telemetry_recorder, **kwargs)
    if plan.retrieval_backend == "pydantic_native":
        return PydanticNativeSearchGateway(telemetry_recorder=telemetry_recorder, **kwargs)
    raise ValueError(f"Unknown retrieval_backend: {plan.retrieval_backend!r}")
