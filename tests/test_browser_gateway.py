"""Tests for BrowserAugmentedGateway, with a fake playwright.async_api so no

real browser is launched. Monkeypatches `playwright.async_api.async_playwright`
before `__aenter__` runs its local `from playwright.async_api import
async_playwright` — the lookup happens at call time, so the patched
attribute is what gets bound.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pydagy_research.browser_gateway import BrowserAugmentedGateway
from pydagy_research.models import EvidenceRecord


class _FakePage:
    def __init__(self, *, title: str = "Page Title", text: str = "Rendered body text", raises: Exception | None = None):
        self._title = title
        self._text = text
        self._raises = raises
        self.closed = False

    async def goto(self, url: str, wait_until: str | None = None, timeout: float | None = None) -> None:
        if self._raises:
            raise self._raises

    async def title(self) -> str:
        return self._title

    async def evaluate(self, script: str) -> str:
        return self._text

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage):
        self._page = page
        self.closed = False
        self.new_page_calls = 0

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        return self._page

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser):
        self._browser = browser
        self.launch_calls = 0

    async def launch(self, headless: bool = True) -> _FakeBrowser:
        self.launch_calls += 1
        return self._browser


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser):
        self.chromium = _FakeChromium(browser)


class _FakeAsyncPlaywrightCM:
    def __init__(self, playwright: _FakePlaywright):
        self._playwright = playwright
        self.exited = False

    async def __aenter__(self) -> _FakePlaywright:
        return self._playwright

    async def __aexit__(self, *exc_info: object) -> None:
        self.exited = True


def _patch_playwright(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> _FakeBrowser:
    browser = _FakeBrowser(page)
    cm = _FakeAsyncPlaywrightCM(_FakePlaywright(browser))
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: cm)
    return browser


class _FakeInnerGateway:
    def __init__(self):
        self.aenter_calls = 0
        self.aexit_calls = 0
        self.read_calls: list[str] = []
        self.search_calls: list[tuple[str, str | None]] = []
        self.fallback_record = EvidenceRecord(
            evidence_id="RAW-fallback",
            source_url="https://example.com/fallback",
            source_kind="page_content",
            title="fallback",
            raw_extract="fallback text",
            timestamp=datetime.now(timezone.utc),
            status="success",
        )

    async def __aenter__(self) -> "_FakeInnerGateway":
        self.aenter_calls += 1
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.aexit_calls += 1

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
        self.search_calls.append((query, domain))
        return []

    async def read(self, url: str) -> list[EvidenceRecord]:
        self.read_calls.append(url)
        return [self.fallback_record]


@pytest.mark.asyncio
async def test_read_uses_rendered_text_on_success(monkeypatch):
    page = _FakePage(title="Rendered Title", text="the real, JS-rendered page content")
    browser = _patch_playwright(monkeypatch, page)
    inner = _FakeInnerGateway()

    async with BrowserAugmentedGateway(inner) as gateway:
        records = await gateway.read("https://example.com/js-heavy")

    assert len(records) == 1
    record = records[0]
    assert record.source_kind == "page_content"
    assert record.title == "Rendered Title"
    assert record.raw_extract == "the real, JS-rendered page content"
    assert record.status == "success"
    assert inner.read_calls == []  # never fell back
    assert browser.closed is True
    assert page.closed is True
    assert inner.aenter_calls == 1
    assert inner.aexit_calls == 1


@pytest.mark.asyncio
async def test_read_falls_back_to_inner_on_render_error(monkeypatch):
    page = _FakePage(raises=RuntimeError("navigation timeout"))
    _patch_playwright(monkeypatch, page)
    inner = _FakeInnerGateway()

    async with BrowserAugmentedGateway(inner) as gateway:
        records = await gateway.read("https://example.com/broken")

    assert inner.read_calls == ["https://example.com/broken"]
    assert records == [inner.fallback_record]


@pytest.mark.asyncio
async def test_read_falls_back_to_inner_on_empty_render(monkeypatch):
    page = _FakePage(text="   ")  # whitespace-only
    _patch_playwright(monkeypatch, page)
    inner = _FakeInnerGateway()

    async with BrowserAugmentedGateway(inner) as gateway:
        records = await gateway.read("https://example.com/empty")

    assert inner.read_calls == ["https://example.com/empty"]
    assert records == [inner.fallback_record]


@pytest.mark.asyncio
async def test_search_delegates_unchanged_to_inner(monkeypatch):
    page = _FakePage()
    _patch_playwright(monkeypatch, page)
    inner = _FakeInnerGateway()

    async with BrowserAugmentedGateway(inner) as gateway:
        await gateway.search("some query", "example.com")

    assert inner.search_calls == [("some query", "example.com")]


@pytest.mark.asyncio
async def test_aenter_failure_still_closes_inner_gateway(monkeypatch):
    def _raise():
        raise RuntimeError("no browser binary installed")

    monkeypatch.setattr("playwright.async_api.async_playwright", _raise)
    inner = _FakeInnerGateway()
    gateway = BrowserAugmentedGateway(inner)

    with pytest.raises(RuntimeError):
        await gateway.__aenter__()

    assert inner.aenter_calls == 1
    assert inner.aexit_calls == 1  # cleaned up despite the failure
