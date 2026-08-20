"""Host-controlled headless-browser fetch (Option B from the JS-rendering discussion).

`AntigravitySDKGateway.read()` (`read_url_content`) and
`PydanticNativeSearchGateway.read()` (`WebFetch`) are both static HTML
fetches — no JavaScript execution. Client-rendered pages (GitHub's security
advisories UI, Snyk's package pages, etc.) come back as generic boilerplate
instead of their real content, which starves the Writer Node of citable
evidence even when the read "succeeded".

`BrowserAugmentedGateway` wraps any other `RetrievalGateway`, replacing its
`.read()` with a real headless-Chromium fetch driven entirely by host Python
code — not a model-mediated tool call. That has two consequences that matter
for PLAN.md's trust boundary:

* No drift risk: there's no inner-model tool call to diverge from the
  requested URL (PLAN.md §2's `@post_tool_call` drift check exists precisely
  because retrieval is normally LLM-mediated; this path isn't).
* No "did the model remember to inspect the cached content" gap: the
  READ_URL_CONTENT -> VIEW_FILE two-step (PLAN.md §1.1) that turned out to be
  unreliable in practice doesn't exist here — one `page.goto()` produces the
  rendered text directly.

`.search()` is delegated unchanged to the wrapped gateway; headless
rendering isn't a search mechanism on its own.

Requires the optional `browser` extra (`uv sync --extra browser` then
`uv run playwright install chromium`) — imported lazily so the rest of the
package has no hard dependency on it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .gateway import RetrievalGateway, _temp_evidence_id
from .models import EvidenceRecord

__all__ = ["BrowserAugmentedGateway"]

_logger = logging.getLogger(__name__)

_MAX_CHARS = 20_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BrowserAugmentedGateway:
    """Wraps `inner` (either backend), rendering `.read()` with headless Chromium.

    One Chromium instance is launched per session (`async with gateway:`) and
    reused across every `.read()` in the plan, then closed on exit — matching
    PLAN.md §3's "one session per plan" IPC-efficiency principle for the
    wrapped gateway's own subprocess (Antigravity's `localharness`, if that's
    what's wrapped).
    """

    def __init__(
        self,
        inner: RetrievalGateway,
        *,
        timeout_s: float = 20.0,
        headless: bool = True,
    ) -> None:
        self._inner = inner
        self._timeout_s = timeout_s
        self._headless = headless
        self._playwright_cm: Any | None = None
        self._playwright: Any | None = None
        self._browser: Any | None = None

    async def __aenter__(self) -> "BrowserAugmentedGateway":
        from playwright.async_api import async_playwright

        await self._inner.__aenter__()
        try:
            self._playwright_cm = async_playwright()
            self._playwright = await self._playwright_cm.__aenter__()
            self._browser = await self._playwright.chromium.launch(headless=self._headless)
        except Exception:
            await self._inner.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright_cm is not None:
            await self._playwright_cm.__aexit__(*exc_info)
            self._playwright_cm = None
        await self._inner.__aexit__(*exc_info)

    # -- RetrievalGateway protocol -----------------------------------------

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
        return await self._inner.search(query, domain)

    async def read(self, url: str) -> EvidenceRecord:
        assert self._browser is not None, "BrowserAugmentedGateway must be used as `async with gateway:`"
        try:
            title, text = await self._render(url)
        except Exception as exc:
            _logger.warning("Headless render failed for %s (%s); falling back to inner gateway", url, exc)
            return await self._inner.read(url)

        if not text.strip():
            _logger.info("Headless render of %s produced no text; falling back to inner gateway", url)
            return await self._inner.read(url)

        return EvidenceRecord(
            evidence_id=_temp_evidence_id(),
            source_url=url,
            source_kind="page_content",
            title=title or url,
            raw_extract=text[:_MAX_CHARS],
            timestamp=_now(),
            status="success",
        )

    async def _render(self, url: str) -> tuple[str, str]:
        page = await self._browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=self._timeout_s * 1000)
            title = await page.title()
            text = await page.evaluate("document.body ? document.body.innerText : ''")
        finally:
            await page.close()
        return title, text
