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
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import EvidenceRecord, ResearchPlan, SearchOrFetchRequest

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


class RetrievalGateway(Protocol):
    """Backend-agnostic retrieval interface (PLAN.md §1)."""

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]: ...

    async def read(self, url: str) -> EvidenceRecord: ...


async def run_plan(gateway: RetrievalGateway, plan: ResearchPlan) -> list[EvidenceRecord]:
    """Executes a `ResearchPlan`'s requests sequentially against `gateway`.

    Requests execute one at a time (PLAN.md §3: one Antigravity session is
    turn-based, not concurrent) regardless of which backend is configured, so
    this helper is backend-agnostic and lives outside both gateway classes.
    """
    records: list[EvidenceRecord] = []
    for request in plan.requests:
        if request.action == "search":
            records.extend(await gateway.search(request.query_or_url, request.domain))
        else:
            records.append(await gateway.read(request.query_or_url))
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
    """

    def __init__(
        self,
        *,
        model: Any | None = None,
        budget_config: Any | None = None,
        agent_config_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._model = model
        self._budget_config = budget_config
        self._agent_config_kwargs = dict(agent_config_kwargs or {})
        self._agent: Any | None = None
        self._agent_cm: Any | None = None
        # id (ToolCall.id) -> args, captured by the pre-tool-call hook so the
        # post-tool-call hook can see what was *actually* executed.
        self._pending_call_args: dict[str, dict[str, Any]] = {}
        self._expected_action: str | None = None
        self._expected_value: str | None = None
        self._turn_records: list[EvidenceRecord] = []

    async def __aenter__(self) -> "AntigravitySDKGateway":
        # Imported lazily: google-antigravity is an optional extra.
        from google.antigravity import Agent, LocalAgentConfig, types
        from google.antigravity.hooks import hooks, policy

        self._ga_types = types

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

        pre_hook = hooks.pre_tool_call_decide(self._pre_tool_call)
        post_hook = hooks.post_tool_call(self._post_tool_call)

        config_kwargs: dict[str, Any] = dict(self._agent_config_kwargs)
        if self._model is not None:
            config_kwargs.setdefault("model", self._model)
        config = LocalAgentConfig(
            capabilities=capabilities,
            policies=policies,
            hooks=[pre_hook, post_hook],
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

    async def read(self, url: str) -> EvidenceRecord:
        prompt = f"Use the read_url_content tool to read exactly this URL: {url!r}."
        records = await self._run_turn(action="read", value=url, prompt=prompt)
        for record in records:
            if record.source_kind == "page_content":
                return record
        return self._missing_call_record(action="read", value=url)

    # -- internals -----------------------------------------------------------

    async def _run_turn(self, *, action: str, value: str, prompt: str) -> list[EvidenceRecord]:
        assert self._agent is not None, "AntigravitySDKGateway must be used as `async with gateway:`"
        types = self._ga_types
        self._expected_action = action
        self._expected_value = value
        self._turn_records = []
        self._pending_call_args.clear()  # defensive: no carry-over if a prior turn errored mid-call
        try:
            response = await self._agent.chat(prompt)
            await response.text()  # drain the stream; evidence is captured by the post_tool_call hook
        except (
            types.AntigravityConnectionError,
            types.AntigravityExecutionError,
            types.ToolExecutionError,
            types.AntigravityValidationError,
        ) as exc:
            _logger.warning("Antigravity gateway turn failed (%s=%r): %s", action, value, exc)
            return [self._failed_record(action=action, value=value, error=str(exc))]
        return list(self._turn_records)

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
        types = self._ga_types
        if data.id and data.name in (
            types.BuiltinTools.SEARCH_WEB.value,
            types.BuiltinTools.READ_URL_CONTENT.value,
        ):
            self._pending_call_args[data.id] = dict(data.args)
        return types.HookResult(allow=True)

    async def _post_tool_call(self, data: Any) -> None:
        """Extracts `EvidenceRecord`s from tool results (PLAN.md §2) and flags drift."""
        types = self._ga_types
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
    ) -> None:
        from pydantic_ai import Agent as PydanticAgent
        from pydantic_ai.capabilities import WebFetch, WebSearch

        self._search_agent: Any = PydanticAgent(
            model, capabilities=[WebSearch(**(search_kwargs or {}))]
        )
        self._fetch_agent: Any = PydanticAgent(
            model, capabilities=[WebFetch(**(fetch_kwargs or {}))]
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
        )
        return records

    async def read(self, url: str) -> EvidenceRecord:
        records = await self._run(
            self._fetch_agent,
            f"Fetch and return the full contents of {url}",
            source_kind="page_content",
            fallback_url=url,
            fallback_title=url,
        )
        if records:
            return records[0]
        return EvidenceRecord(
            evidence_id=_temp_evidence_id(),
            source_url=url,
            source_kind="page_content",
            title=url,
            raw_extract="",
            timestamp=_now(),
            status="failed",
            error="no content returned",
        )

    async def _run(
        self,
        agent: Any,
        prompt: str,
        *,
        source_kind: str,
        fallback_url: str,
        fallback_title: str,
    ) -> list[EvidenceRecord]:
        from pydantic_ai.exceptions import AgentRunError

        try:
            result = await agent.run(prompt)
        except AgentRunError as exc:
            _logger.warning("Native search/fetch turn failed (%s): %s", fallback_url, exc)
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
        return _extract_native_evidence(
            result, source_kind=source_kind, fallback_url=fallback_url, fallback_title=fallback_title
        )


def _extract_native_evidence(
    result: Any, *, source_kind: str, fallback_url: str, fallback_title: str
) -> list[EvidenceRecord]:
    """Best-effort per-source extraction from native tool-call grounding metadata.

    Falls back to one opaque `raw_extract` record built from `result.output`
    when the provider doesn't itemize sources (PLAN.md §1).
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


def make_gateway(plan: ResearchPlan, **kwargs: Any) -> RetrievalGateway:
    """Instantiates the `RetrievalGateway` selected by `plan.retrieval_backend` (PLAN.md §1)."""
    if plan.retrieval_backend == "antigravity":
        return AntigravitySDKGateway(**kwargs)
    if plan.retrieval_backend == "pydantic_native":
        return PydanticNativeSearchGateway(**kwargs)
    raise ValueError(f"Unknown retrieval_backend: {plan.retrieval_backend!r}")
