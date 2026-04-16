"""OpenAI-compatible provider adapters (Groq, Ollama, generic OpenAI-compatible).

Each subclass overrides provider_name, default base_url, and optionally
model listing behavior. The core streaming/tool logic is inherited from
OpenAIAdapter. The generic ``openai_compatible`` adapter covers any
self-hosted OpenAI-compatible server (LM Studio, LiteLLM, vLLM, …) — the
admin supplies the base URL.
"""

import logging
from core.layers.providers.openai_adapter import OpenAIAdapter

logger = logging.getLogger("direct-runner")


class GroqAdapter(OpenAIAdapter):
    """Groq Cloud API — OpenAI-compatible, fast inference."""

    @property
    def provider_name(self) -> str:
        return "groq"

    def _get_base_url(self, endpoint_url: str | None) -> str:
        return endpoint_url or "https://api.groq.com/openai/v1"

    def _extra_api_kwargs(self, model: str, has_tools: bool) -> dict:
        # Qwen3 models on Groq are hybrid thinkers that THINK by default and
        # stream the reasoning as raw ``<think>`` text inside message.content.
        # The platform ships qwen as its fast non-reasoning Groq tier, so pin
        # thinking off — Groq's qwen vocabulary is "none"/"default" (the
        # platform effort scale doesn't map onto it).
        if model.startswith("qwen"):
            return {"reasoning_effort": "none"}
        return {}


class OllamaAdapter(OpenAIAdapter):
    """Ollama local inference — OpenAI-compatible endpoint."""

    @property
    def provider_name(self) -> str:
        return "ollama"

    def _get_base_url(self, endpoint_url: str | None) -> str:
        return endpoint_url or "http://localhost:11434/v1"

    def _get_default_api_key(self) -> str:
        return "ollama"  # Ollama doesn't require auth but SDK needs a value

    async def list_available_models(
        self,
        api_key: str,
        endpoint_url: str | None = None,
    ) -> list[dict]:
        """Fetch models from Ollama's native /api/tags endpoint."""
        import httpx

        base = (endpoint_url or "http://localhost:11434").rstrip("/")
        url = f"{base}/api/tags"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                models = []
                for m in data.get("models", []):
                    name = m.get("name", "")
                    if name:
                        models.append({
                            "model_id": name,
                            "display_name": name,
                        })
                return sorted(models, key=lambda x: x["model_id"])
        except Exception as e:
            logger.warning(f"Ollama model list failed: {e}")
            return []


class OpenAICompatibleAdapter(OpenAIAdapter):
    """Generic self-hosted OpenAI-compatible endpoint (LM Studio, LiteLLM, vLLM, …).

    The admin always supplies the base URL (local subscriptions require an
    ``endpoint_url``), so the default below is only a fallback.
    """

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    def _get_base_url(self, endpoint_url: str | None) -> str:
        return endpoint_url or "http://localhost:4000/v1"

    def _get_default_api_key(self) -> str:
        return "not-needed"  # many local servers ignore it; SDK needs a value
