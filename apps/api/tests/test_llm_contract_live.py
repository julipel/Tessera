"""Контрактный тест LLM-адаптеров на реальных провайдерах (маркер `live`, не в `make test`).

Запуск: `OPENAI_API_KEY=... LIVE_OPENAI_MODEL=... ANTHROPIC_API_KEY=... LIVE_ANTHROPIC_MODEL=...
OPENAI_COMPATIBLE_API_KEY=... LIVE_OPENAI_COMPATIBLE_MODEL=... uv run pytest -m live`
(для `openai_compatible` — модель, которая вызывает инструменты через Chat Completions).
Провайдер без ключа или модели пропускается. Новый адаптер добавляется в `PROVIDERS`.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from app.modules.agent.public import (
    LLMChunk,
    LLMClient,
    LLMRequest,
    ResponseCompleted,
    StopReason,
    TextDelta,
    ToolCallStarted,
    ToolResultMessage,
    ToolSchema,
    UserMessage,
    create_anthropic_llm,
    create_openai_llm,
)
from app.settings import Settings

pytestmark = pytest.mark.live


@dataclass(frozen=True)
class Provider:
    name: str
    model_env: str
    factory: Callable[[Settings], LLMClient | None]


def _openai(settings: Settings) -> LLMClient | None:
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    if not key:
        return None
    return create_openai_llm(key, base_url=settings.openai_base_url)


def _openai_compatible(settings: Settings) -> LLMClient | None:
    secret = settings.openai_compatible_api_key
    key = secret.get_secret_value() if secret else ""
    if not key:
        return None
    return create_openai_llm(key, base_url=settings.openai_compatible_base_url)


def _anthropic(settings: Settings) -> LLMClient | None:
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if not key:
        return None
    return create_anthropic_llm(key, base_url=settings.anthropic_base_url)


PROVIDERS = [
    Provider("openai", "LIVE_OPENAI_MODEL", _openai),
    Provider("openai_compatible", "LIVE_OPENAI_COMPATIBLE_MODEL", _openai_compatible),
    Provider("anthropic", "LIVE_ANTHROPIC_MODEL", _anthropic),
]


@dataclass(frozen=True)
class Live:
    llm: LLMClient
    model: str


@pytest.fixture(params=PROVIDERS, ids=lambda provider: provider.name)
def live(request: pytest.FixtureRequest) -> Live:
    provider: Provider = request.param
    model = os.environ.get(provider.model_env)
    llm = provider.factory(Settings())
    if llm is None or not model:
        pytest.skip(f"{provider.name}: нет ключа API или {provider.model_env}")
    return Live(llm, model)


STOCK = ToolSchema(
    name="get_stock",
    description="Остаток товара на складе по артикулу. Вызывай всегда, когда спрашивают остаток.",
    parameters={
        "type": "object",
        "properties": {"sku": {"type": "string", "description": "Артикул"}},
        "required": ["sku"],
        "additionalProperties": False,
    },
)


async def collect(llm: LLMClient, request: LLMRequest) -> list[LLMChunk]:
    return [chunk async for chunk in llm.stream(request)]


def completed(chunks: list[LLMChunk]) -> ResponseCompleted:
    last = chunks[-1]
    assert isinstance(last, ResponseCompleted)
    return last


async def test_text_answer_streams_and_completes(live: Live) -> None:
    request = LLMRequest(
        model=live.model,
        system="Отвечай одним коротким предложением.",
        messages=(UserMessage("Скажи «привет»."),),
        max_output_tokens=200,
    )

    chunks = await collect(live.llm, request)

    response = completed(chunks).response
    deltas = [chunk.text for chunk in chunks if isinstance(chunk, TextDelta)]
    assert deltas
    assert "".join(deltas) == response.text
    assert response.stop_reason is StopReason.END_TURN
    assert response.tool_calls == ()
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0


async def test_tool_call_round_trip(live: Live) -> None:
    question = UserMessage("Какой остаток у артикула A-17? Используй инструмент.")
    first = LLMRequest(
        model=live.model,
        system="Ты — консультант магазина. Остатки узнавай только через инструменты.",
        messages=(question,),
        tools=(STOCK,),
        max_output_tokens=500,
    )

    chunks = await collect(live.llm, first)

    response = completed(chunks).response
    assert response.stop_reason is StopReason.TOOL_CALLS
    (call,) = response.tool_calls
    assert call.name == "get_stock"
    assert call.arguments == {"sku": "A-17"}
    assert ToolCallStarted(id=call.id, name=call.name) in chunks

    second = LLMRequest(
        model=live.model,
        system=first.system,
        messages=(
            question,
            response.as_message(),
            ToolResultMessage(tool_call_id=call.id, content='{"sku": "A-17", "stock": 42}'),
        ),
        tools=(STOCK,),
        max_output_tokens=500,
    )

    final = completed(await collect(live.llm, second)).response
    assert final.stop_reason is StopReason.END_TURN
    assert "42" in final.text
