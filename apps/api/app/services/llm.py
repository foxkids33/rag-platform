from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.core.config import settings


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMOutput:
    text: str
    model: str
    finish_reason: str | None


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


class LLMClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(settings.llm_timeout_seconds)
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    @staticmethod
    def _headers() -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.vllm_api_key:
            headers["Authorization"] = f"Bearer {settings.vllm_api_key}"
        return headers

    @staticmethod
    def _payload(
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict:
        if not settings.vllm_model:
            raise LLMError("VLLM_MODEL is not configured")
        return {
            "model": settings.vllm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> LLMOutput:
        payload = self._payload(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )
        try:
            response = await self._get_client().post(
                chat_completions_url(settings.vllm_base_url),
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"LLM service is unavailable: {exc}") from exc

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM returned an unexpected response") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM returned an empty answer")

        return LLMOutput(
            text=content.strip(),
            model=str(data.get("model") or settings.vllm_model),
            finish_reason=choice.get("finish_reason"),
        )

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        try:
            async with self._get_client().stream(
                "POST",
                chat_completions_url(settings.vllm_base_url),
                headers=self._headers(),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw_data = line[5:].strip()
                    if raw_data == "[DONE]":
                        break
                    try:
                        event = json.loads(raw_data)
                        delta = event["choices"][0]["delta"].get("content")
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        raise LLMError("LLM returned an invalid stream event") from exc
                    if isinstance(delta, str) and delta:
                        yield delta
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM service is unavailable: {exc}") from exc

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


llm = LLMClient()
