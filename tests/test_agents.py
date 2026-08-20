"""Tests for the Planner/Writer agent factories (agents.py)."""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.models.test import TestModel

from pydagy_research.agents import build_planner_agent, planner_prompt


@pytest.mark.asyncio
async def test_planner_agent_defaults_to_search_enabled_and_test_model_rejects_it():
    """Regression guard for the `enable_search=False` escape hatch tests rely on:

    `TestModel` raises `UserError` for *any* configured capability, whether
    or not it's actually invoked (confirmed live: `Agent(TestModel(),
    capabilities=[WebSearch()]).run(...)` fails immediately). If
    `build_planner_agent`'s default ever silently flips to
    `enable_search=False`, every other test that explicitly passes
    `enable_search=False` would still pass but would stop testing anything
    -- this test fails loudly instead if that default ever drifts.
    """
    agent = build_planner_agent(TestModel())
    with pytest.raises(UserError):
        await agent.run(planner_prompt("irrelevant"))


@pytest.mark.asyncio
async def test_planner_agent_with_search_disabled_runs_on_test_model():
    agent = build_planner_agent(TestModel(), enable_search=False)
    result = await agent.run(planner_prompt("What is the capital of France?"))
    assert result.output.question  # TestModel auto-populates a valid ResearchPlan
