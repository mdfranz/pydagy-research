"""Tests for optional Logfire tracing (tracing.py)."""

from __future__ import annotations

from pydagy_research.tracing import configure_tracing


def test_configure_tracing_is_a_noop_without_logfire_token(monkeypatch):
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    assert configure_tracing() is False


def test_configure_tracing_configures_logfire_when_token_present(monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "test-token-not-a-real-credential")
    calls: list[str] = []

    import logfire

    def fake_configure(**kwargs):
        calls.append("configure")

    def fake_instrument_pydantic_ai(**kwargs):
        calls.append("instrument_pydantic_ai")

    monkeypatch.setattr(logfire, "configure", fake_configure)
    monkeypatch.setattr(logfire, "instrument_pydantic_ai", fake_instrument_pydantic_ai)

    assert configure_tracing() is True
    assert calls == ["configure", "instrument_pydantic_ai"]
