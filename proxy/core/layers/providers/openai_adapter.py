"""OpenAI provider adapter for the Direct LLM path.

Handles OpenAI-format API interaction: system messages, function-calling
tools, streaming chat completions, and tool_calls message format.

Also serves as the base for OpenAI-compatible providers (Groq, Ollama,
LM Studio) via subclassing.
"""

import logging
from typing import AsyncIterator

import config as app_config
from core.layers.providers.base import (
    ProviderAdapter, ProviderStreamEvent, ProviderUsage, ProviderError,
)

logger = logging.getLogger("direct-runner")


class OpenAIAdapter(ProviderAdapter):
    """OpenAI Chat Completions API adapter."""

    @property
    def provider_name(self) -> str:
        return "openai"

    def _get_base_url(self, endpoint_url: str | None) -> str | None:
        """Base URL for the API. Override in subclasses for compat providers."""
        return endpoint_url  # None = OpenAI default

    def _get_default_api_key(self) -> str | None:
        """Default API key when none provided. Override for local providers."""
        return None

    def _extra_api_kwargs(self, model: str, has_tools: bool) -> dict:
        """Provider-specific chat-completion params merged into every request.
        Override in subclasses (e.g. Groq pins qwen models to non-thinking)."""
        return {}

    @staticmethod
    def _decompose_usage(usage) -> ProviderUsage:
        """OpenAI usage object → ProviderUsage in the convention
        ``calculate_cost`` expects (Anthropic-style: ``input_tokens`` = tokens
        billed at the plain input rate only).

        OpenAI's ``prompt_tokens`` INCLUDES cache reads and (gpt-5.6+) cache
        writes, so both are subtracted out and reported on their own fields:

        - ``prompt_tokens_details.cached_tokens`` — read from cache, billed at
          the 90%-discount rate (also what Groq reports for its automatic
          50%-discount caching; the discount itself lives in the pricing tuple).
        - ``prompt_tokens_details.cache_write_tokens`` — gpt-5.6+ only: writes
          are billed at 1.25x the input rate (implicit AND explicit caching)
          and reported per call. The pinned SDK's typed model doesn't declare
          the field yet, but pydantic ``extra="allow"`` keeps it addressable
          via getattr; pre-5.6 models never send it (writes were free there).
        """
        cached = 0
        written = 0
        ptd = getattr(usage, "prompt_tokens_details", None)
        if ptd:
            cached = getattr(ptd, "cached_tokens", 0) or 0
            written = getattr(ptd, "cache_write_tokens", 0) or 0
        total_prompt = usage.prompt_tokens or 0
        return ProviderUsage(
            input_tokens=max(0, total_prompt - cached - written),
            output_tokens=usage.completion_tokens or 0,
            cache_read_tokens=cached,
            cache_write_tokens=written,
        )

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert MCP-format tools to OpenAI function-calling format.

        MCP format: {"name", "description", "input_schema"}
        OpenAI format: {"type": "function", "function": {"name", "description", "parameters"}}
        """
        openai_tools = []
        for t in tools:
            if "input_schema" not in t:
                continue  # skip server-side tools (Anthropic-only)
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            })
        return openai_tools

    async def stream_response(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        endpoint_url: str | None = None,
        effort: str = "",
    ) -> AsyncIterator[ProviderStreamEvent]:
        from openai import AsyncOpenAI, APIError

        effective_key = api_key or self._get_default_api_key() or ""
        base_url = self._get_base_url(endpoint_url)

        client_kwargs: dict = {"api_key": effective_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = AsyncOpenAI(**client_kwargs)

        try:
            # Build messages: system prompt first, then conversation
            api_messages: list[dict] = [
                {"role": "system", "content": system_prompt},
            ]
            api_messages.extend(messages)

            # Convert tools to OpenAI format
            openai_tools = self._convert_tools(tools)

            # OpenAI's newer models (o-series, gpt-4.1+) require
            # max_completion_tokens instead of max_tokens.
            # Use max_completion_tokens universally — it works for all
            # current OpenAI models and is the forward-compatible option.
            api_kwargs: dict = {
                "model": model,
                "messages": api_messages,
                "max_completion_tokens": max_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if openai_tools:
                api_kwargs["tools"] = openai_tools

            # Reasoning effort — only for reasoning-capable models WITHOUT tools.
            # OpenAI's /v1/chat/completions does not support reasoning_effort
            # with function tools (returns 400). Only pass when no tools are present.
            # Platform "xhigh", "max" AND "ultra" all collapse onto OpenAI's
            # "xhigh" (the top of the chat-completions scale; "ultra" is a
            # Codex-CLI orchestration mode — even codex-rs sends the API "max").
            if effort and not openai_tools:
                _EFFORT_TO_OPENAI = {
                    "low": "low",
                    "medium": "medium",
                    "high": "high",
                    "xhigh": "xhigh",
                    "max": "xhigh",
                    "ultra": "xhigh",
                }
                openai_effort = _EFFORT_TO_OPENAI.get(effort)
                if openai_effort and app_config.model_supports_reasoning(model):
                    api_kwargs["reasoning_effort"] = openai_effort

            api_kwargs.update(self._extra_api_kwargs(model, bool(openai_tools)))

            # Track accumulated content for raw_content event
            accumulated_text = ""
            # Track tool calls by index (OpenAI streams them incrementally)
            tool_call_acc: dict[int, dict] = {}

            try:
                stream = await client.chat.completions.create(**api_kwargs)

                async for chunk in stream:
                    # Usage-only chunk (last chunk with stream_options.include_usage)
                    if not chunk.choices:
                        if chunk.usage:
                            yield ProviderStreamEvent(
                                type="usage",
                                usage=self._decompose_usage(chunk.usage),
                            )
                        continue

                    choice = chunk.choices[0]
                    delta = choice.delta
                    finish = choice.finish_reason

                    # Text content
                    if delta and delta.content:
                        accumulated_text += delta.content
                        yield ProviderStreamEvent(
                            type="text_delta", text=delta.content,
                        )

                    # Tool call deltas
                    if delta and delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_call_acc:
                                tool_call_acc[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "arguments": "",
                                }
                            tc = tool_call_acc[idx]

                            # ID arrives in first chunk for this index
                            if tc_delta.id:
                                tc["id"] = tc_delta.id

                            # Function name arrives in first chunk
                            if tc_delta.function and tc_delta.function.name:
                                tc["name"] = tc_delta.function.name
                                yield ProviderStreamEvent(
                                    type="tool_start",
                                    tool_name=tc["name"],
                                    tool_id=tc["id"],
                                )

                            # Arguments stream incrementally
                            if tc_delta.function and tc_delta.function.arguments:
                                tc["arguments"] += tc_delta.function.arguments
                                yield ProviderStreamEvent(
                                    type="tool_input_delta",
                                    tool_input_json=tc_delta.function.arguments,
                                )

                    # Finish reason
                    if finish:
                        # Emit tool_stop events
                        if finish == "tool_calls":
                            for idx in sorted(tool_call_acc.keys()):
                                tc = tool_call_acc[idx]
                                yield ProviderStreamEvent(
                                    type="tool_stop",
                                    tool_name=tc["name"],
                                    tool_id=tc["id"],
                                    tool_input_json=tc["arguments"],
                                )

                        yield ProviderStreamEvent(
                            type="stop",
                            stop_reason=(
                                "tool_use" if finish == "tool_calls" else finish
                            ),
                        )

            except APIError as e:
                raise ProviderError(
                    message=str(e),
                    status_code=getattr(e, "status_code", 0),
                    retryable=getattr(e, "status_code", 0) in (429, 500, 502, 503),
                )

            # Yield raw content for message serialization
            raw = {
                "content": accumulated_text or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in (
                        tool_call_acc[i]
                        for i in sorted(tool_call_acc.keys())
                    )
                ] if tool_call_acc else [],
            }
            yield ProviderStreamEvent(type="content", raw_content=raw)

        finally:
            await client.close()

    def format_tool_results(self, results: list[dict]) -> list[dict]:
        """OpenAI format: separate tool messages per result."""
        return [
            {
                "role": "tool",
                "tool_call_id": r["tool_use_id"],
                "content": r["content"],
            }
            for r in results
        ]

    def serialize_assistant_content(self, raw_content) -> str | dict:
        """Serialize OpenAI response for message history.

        Returns str for simple text, or dict with content + tool_calls
        for tool-calling responses (merged into the assistant message).
        """
        if isinstance(raw_content, dict):
            text = raw_content.get("content") or ""
            tcs = raw_content.get("tool_calls", [])
            if not tcs:
                return text
            # Return dict — run_direct_stream merges into assistant message
            result: dict = {"content": text or None}
            result["tool_calls"] = tcs
            return result
        return str(raw_content)

    async def list_available_models(
        self,
        api_key: str,
        endpoint_url: str | None = None,
    ) -> list[dict]:
        """Fetch models from OpenAI API."""
        from openai import AsyncOpenAI

        client_kwargs: dict = {"api_key": api_key}
        base_url = self._get_base_url(endpoint_url)
        if base_url:
            client_kwargs["base_url"] = base_url

        client = AsyncOpenAI(**client_kwargs)
        try:
            response = await client.models.list()
            models = []
            for m in response.data:
                # Filter to chat-capable models (skip embeddings, tts, etc.)
                mid = m.id
                if any(skip in mid for skip in (
                    "embedding", "tts", "whisper", "dall-e",
                    "moderation", "davinci", "babbage",
                )):
                    continue
                models.append({
                    "model_id": mid,
                    "display_name": mid,
                })
            return sorted(models, key=lambda x: x["model_id"])
        finally:
            await client.close()
