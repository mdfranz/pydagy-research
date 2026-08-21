"""Factory for creating MultiProviderGateway instances (MULTI-PROVIDER-PLAN.md §3).

Builds a MultiProviderGateway from a comma-separated provider list and model mapping.
"""

from __future__ import annotations

from typing import Any

from .gateway import make_gateway, RetrievalGateway
from .multi_provider_gateway import MultiProviderGateway
from .models import ResearchPlan

__all__ = ["make_multi_provider_gateway"]


def make_multi_provider_gateway(
    plan: ResearchPlan,
    providers: list[str],
    model_map: dict[str, str],
    **kwargs: Any,
) -> RetrievalGateway:
    """Create a MultiProviderGateway wrapping N single-provider gateways.

    Args:
        plan: ResearchPlan (used to determine backend for each provider)
        providers: List of provider names (e.g., ["gemini", "anthropic"])
        model_map: {provider_name: model_id, ...} mapping
            e.g., {"gemini": "google:gemini-3.7-flash", "anthropic": "anthropic:claude-opus-5"}
        **kwargs: Additional kwargs passed to make_gateway()

    Returns:
        MultiProviderGateway wrapping gateways for all specified providers

    Example:
        ```python
        gateway = make_multi_provider_gateway(
            plan,
            providers=["gemini", "anthropic"],
            model_map={
                "gemini": "google:gemini-3.7-flash",
                "anthropic": "anthropic:claude-opus-5",
            }
        )
        ```
    """
    gateways: dict[str, RetrievalGateway] = {}

    for provider in providers:
        if provider not in model_map:
            raise ValueError(f"Provider {provider!r} not in model_map")

        model_id = model_map[provider]
        # Each provider uses pydantic_native backend
        plan_for_provider = plan.model_copy(update={"retrieval_backend": "pydantic_native"})

        gw = make_gateway(plan_for_provider, model=model_id, **kwargs)
        gateways[provider] = gw

    # OpenAI can't do WebFetch, so mark it as not read-capable if present
    read_capable = {p for p in providers if p != "openai"}

    return MultiProviderGateway(gateways, read_capable=read_capable)
