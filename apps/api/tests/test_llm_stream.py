import asyncio
import json

import httpx
import pytest

from app.services.llm import LLMClient, LLMStreamEnd, settings


@pytest.mark.parametrize("enable_thinking", [None, False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_thinking_template_override_is_optional_and_preserves_model_alias(
    monkeypatch, enable_thinking, stream,
):
    monkeypatch.setattr(settings, "vllm_model", "corporate-llm")
    monkeypatch.setattr(settings, "llm_enable_thinking", enable_thinking)
    messages = [{"role": "user", "content": "Answer using the supplied context."}]
    payload = LLMClient._payload(messages, max_tokens=2048, temperature=0, stream=stream)
    expected = {
        "model": "corporate-llm", "messages": messages,
        "max_tokens": 2048, "temperature": 0, "stream": stream,
    }
    if enable_thinking is not None:
        expected["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    assert payload == expected


@pytest.mark.parametrize("reason", ["stop", "length", None])
def test_stream_reports_provider_finish_reason(monkeypatch, reason):
    monkeypatch.setattr(settings, "vllm_model", "test-model")
    events = [
        {"choices": [{"delta": {"content": "Ответ [1]"}, "finish_reason": None}]},
        {"choices": [{"delta": None, "finish_reason": reason}]},
        {"choices": [], "usage": {"completion_tokens": 5}},
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"

    async def run():
        client = LLMClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)
        )) as transport:
            client._client = transport
            return [event async for event in client.stream_chat([], max_tokens=800, temperature=0)]

    assert asyncio.run(run()) == ["Ответ [1]", *([LLMStreamEnd(reason)] if reason else [])]
