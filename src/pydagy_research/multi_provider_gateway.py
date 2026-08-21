"""Multi-provider retrieval gateway orchestration (MULTI-PROVIDER-PLAN.md §3).

`MultiProviderGateway` fans out search and read requests to multiple
`RetrievalGateway` instances concurrently, tags results with provider
attribution, and returns ALL successful records (no arbitration).

This enables corroboration: the Writer sees independent provider results
for the same query/URL and can evaluate agreement or divergence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from .models import EvidenceRecord, ResearchPlan, SearchOrFetchRequest
from .gateway import RetrievalGateway

__all__ = ["MultiProviderGateway"]

_logger = logging.getLogger(__name__)


class MultiProviderGateway:
    """Fan out search/read to multiple providers, keeping all successful results.

    Wraps N `RetrievalGateway` instances and:
    - Fans out `search()` to ALL providers concurrently
    - Fans out `read()` only to read-capable providers
    - Tags every record with `provider` attribution
    - Records all attempts (success, failed, thin) for transparency
    - Returns ALL successful records (no single-provider arbitration)

    Example:
        ```python
        gateways = {
            "gemini": PydanticNativeSearchGateway(model="google:gemini-3.7-flash"),
            "anthropic": PydanticNativeSearchGateway(model="anthropic:claude-opus-5"),
        }
        multi = MultiProviderGateway(gateways)
        async with multi:
            results = await multi.search("python version", domain="python.org")
            # Returns records from both providers, tagged with provider="gemini"/"anthropic"
        ```
    """

    def __init__(
        self,
        gateways: dict[str, RetrievalGateway],
        *,
        read_capable: set[str] | None = None,
    ) -> None:
        """Initialize with a dict of named gateways.

        Args:
            gateways: {provider_name: gateway_instance, ...}
            read_capable: Set of provider names that support read(). If None,
                assume all can read (caller should specify if some can't).
        """
        self._gateways = gateways
        self._read_capable = read_capable or set(gateways.keys())
        self.attempts: list = []  # Will hold SourceAttempt records

    async def __aenter__(self) -> "MultiProviderGateway":
        """Enter context manager: open all wrapped gateways."""
        tasks = [gw.__aenter__() for gw in self._gateways.values()]
        await asyncio.gather(*tasks)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Exit context manager: close all wrapped gateways."""
        tasks = [gw.__aexit__(*exc_info) for gw in self._gateways.values()]
        await asyncio.gather(*tasks)

    async def search(self, query: str, domain: str | None = None) -> list[EvidenceRecord]:
        """Fan out search to all providers, return all results tagged by provider.

        Args:
            query: Search query
            domain: Optional domain restriction

        Returns:
            All successful records from all providers, each tagged with provider name.
        """
        _logger.info(
            "Multi-provider search: query=%r, domain=%r, providers=%s",
            query,
            domain,
            list(self._gateways.keys()),
        )

        # Fan out to all providers concurrently
        tasks = [
            self._search_one_provider(name, gw, query, domain)
            for name, gw in self._gateways.items()
        ]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all successful records
        records: list[EvidenceRecord] = []
        for provider_name, result in zip(self._gateways.keys(), all_results):
            if isinstance(result, Exception):
                _logger.warning("Search failed for provider %s: %s", provider_name, result)
                continue
            records.extend(result)

        _logger.info(
            "Multi-provider search completed: got %d records from %d providers",
            len(records),
            len(self._gateways),
        )
        return records

    async def read(self, url: str) -> list[EvidenceRecord]:
        """Fan out read to read-capable providers, return all results tagged by provider.

        Args:
            url: URL to fetch

        Returns:
            All successful records from read-capable providers, each tagged with provider name.
        """
        _logger.info(
            "Multi-provider read: url=%r, read_capable=%s",
            url,
            self._read_capable,
        )

        # Fan out only to read-capable providers
        tasks = [
            self._read_one_provider(name, self._gateways[name], url)
            for name in self._read_capable
            if name in self._gateways
        ]

        if not tasks:
            _logger.warning("No read-capable providers configured for url=%r", url)
            return []

        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all successful records
        records: list[EvidenceRecord] = []
        for provider_name, result in zip(self._read_capable, all_results):
            if isinstance(result, Exception):
                _logger.warning("Read failed for provider %s: %s", provider_name, result)
                continue
            records.extend(result)

        _logger.info(
            "Multi-provider read completed: got %d records from %d providers",
            len(records),
            len(self._read_capable),
        )
        return records

    async def _search_one_provider(
        self, provider_name: str, gateway: RetrievalGateway, query: str, domain: str | None
    ) -> list[EvidenceRecord]:
        """Call one provider's search and tag results."""
        try:
            records = await gateway.search(query, domain)
            # Tag each record with provider
            for record in records:
                record.provider = provider_name
            return records
        except Exception as e:
            _logger.exception("Provider %s search failed", provider_name)
            raise

    async def _read_one_provider(
        self, provider_name: str, gateway: RetrievalGateway, url: str
    ) -> list[EvidenceRecord]:
        """Call one provider's read and tag results."""
        try:
            records = await gateway.read(url)
            # Tag each record with provider
            for record in records:
                record.provider = provider_name
            return records
        except Exception as e:
            _logger.exception("Provider %s read failed", provider_name)
            raise
