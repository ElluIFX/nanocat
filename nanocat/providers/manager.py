"""Provider lifecycle — creation, caching, and invalidation.

All code that needs an LLM provider reads from this module.  The global
runtime config is the single source of truth for model selection; callers
do not pass model strings around.
"""

from __future__ import annotations

from typing import Any

from nanocat.config.loader import get_runtime_config
from nanocat.providers.base import GenerationSettings, LLMProvider

_providers: dict[str, LLMProvider] = {}


def make_provider(
    override_model: str | None = None,
    *,
    config: Any | None = None,
) -> LLMProvider:
    """Create a provider for a model using an explicit runtime config."""
    from nanocat.providers.custom_provider import CustomProvider
    from nanocat.providers.deepseek_provider import DeepSeekProvider
    from nanocat.providers.litellm_provider import LiteLLMProvider
    from nanocat.providers.openai_codex_provider import OpenAICodexProvider
    from nanocat.providers.registry import find_by_name

    config = config or get_runtime_config()
    model = override_model or config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    provider_cfg = config.get_provider(model)

    if provider_name == "deepseek":
        provider = DeepSeekProvider(
            api_key=provider_cfg.api_key if provider_cfg else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=provider_cfg.extra_headers if provider_cfg else None,
        )
    elif provider_name == "openai_codex" or model.startswith("openai-codex/"):
        provider = OpenAICodexProvider(default_model=model)
    elif provider_name == "custom":
        provider = CustomProvider(
            api_key=provider_cfg.api_key if provider_cfg else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
            extra_headers=provider_cfg.extra_headers if provider_cfg else None,
        )
    else:
        spec = find_by_name(provider_name)
        if (
            not model.startswith("bedrock/")
            and not (provider_cfg and provider_cfg.api_key)
            and not (spec and (spec.is_oauth or spec.is_local))
        ):
            raise RuntimeError(
                f"No API key configured for provider '{provider_name}'. "
                "Set it in ~/.nanocat/config.json under providers."
            )
        provider = LiteLLMProvider(
            api_key=provider_cfg.api_key if provider_cfg else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=provider_cfg.extra_headers if provider_cfg else None,
            provider_name=provider_name,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


def get_provider(model: str | None = None) -> LLMProvider:
    """Return a cached provider for *model*, creating one if necessary.

    When *model* is None the main model from the global config is used.
    """
    key = model or get_runtime_config().agents.defaults.model
    if key not in _providers:
        _providers[key] = make_provider(override_model=key)
    return _providers[key]


def clear_provider_cache() -> None:
    """Drop every cached provider so the next access rebuilds from config."""
    _providers.clear()
