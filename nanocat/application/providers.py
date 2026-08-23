"""Runtime-scoped provider cache facade."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from nanocat.providers.base import LLMProvider
from nanocat.providers.manager import make_provider


class RuntimeProviderResolver:
    """Own provider instances for one runtime composition.

    Provider construction still delegates to the legacy adapter factory during
    migration, but cache ownership is no longer coupled to the process-global
    provider dictionary.
    """

    def __init__(self, config: Any):
        self._config = config
        self._providers: dict[str, LLMProvider] = {}

    def resolve(self, model: str) -> LLMProvider:
        if model not in self._providers:
            self._providers[model] = make_provider(override_model=model, config=self._config)
        return self._providers[model]

    def clear(self) -> None:
        self._providers.clear()

    async def reconfigure(self) -> None:
        """Close cached adapters so subsequent turns use the current provider config."""
        providers = list(self._providers.values())
        self._providers.clear()
        for provider in providers:
            close = getattr(provider, "aclose", None) or getattr(provider, "close", None)
            if close is None:
                continue
            result = close()
            if hasattr(result, "__await__"):
                await result

    def update_reasoning_effort(self, value: str | None) -> None:
        """Apply a new reasoning effort to providers already owned by this runtime."""
        for provider in self._providers.values():
            provider.generation = replace(provider.generation, reasoning_effort=value)

    async def close(self) -> None:
        """Close adapters that expose an async lifecycle hook."""
        await self.reconfigure()
