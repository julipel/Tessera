# ADR-0010: Провайдер `openai` работает через Responses API, Chat Completions — `openai_compatible`

- Статус: принято
- Дата: 2026-10-03

## Контекст
Адаптер OpenAI (P2-06) работает через Chat Completions. Через него же можно ходить в
OpenAI-совместимые API (RouterAI, OpenRouter, локальные серверы): `OPENAI_BASE_URL`.

В P2-09 выяснилось, что актуальные модели OpenAI вызывают инструменты только через Responses API.
По документации OpenAI (model guidance, сверено через Context7):
- GPT-6 Astra и GPT-6.1 Sol поддерживают Chat Completions, но для вызова инструментов нужен
  Responses API.
- GPT-6 Sol и GPT-6 Luna вызывают функции в Chat Completions только с `reasoning_effort: "none"`,
  то есть без рассуждений.

Это подтвердил запрос к `gpt-6.1-sol` (модель demo-beauty) с `tools` через Chat Completions:
`400 invalid_request_error`, `param: reasoning_effort`, «Function tools with reasoning_effort are
not supported for gpt-6.1-sol in /v1/chat/completions. To use function tools, use /v1/responses».
Пока `update_dialog_state` выключен в demo-beauty, а без инструментов не будет ни каталога, ни
знаний, ни форм (P3). Ядро должно поддерживать модели, которые провайдер рекомендует.

Ещё одна особенность Responses API: у reasoning-моделей между шагами с вызовом инструментов
нужно возвращать модели её reasoning items. Иначе модель теряет ход рассуждения. Без хранения
ответов у OpenAI (`store: false`) это делается через `reasoning.encrypted_content`: элемент
нужно вернуть во входе следующего запроса ровно в том виде, в каком он пришёл. Сейчас порт
`LLMClient` (ADR-0002) такие данные передать не может. `AssistantMessage` несёт только текст и
вызовы инструментов, а клиент без состояния (architecture.md §5).

## Решение
1. **`provider: openai` — новый адаптер `OpenAIResponsesLLM` на Responses API.** Он не хранит
   состояние: `store: false`, `include: ["reasoning.encrypted_content"]`, полный контекст в
   `input` на каждом шаге. `previous_response_id` не используем. Стрим разбирается по событиям
   `response.output_text.delta`, `response.output_item.added` (`function_call` → ранний
   `ToolCallStarted`), `response.function_call_arguments.delta`, `response.output_item.done`
   (готовый элемент, в том числе reasoning с полным `encrypted_content`) и `response.completed`
   (usage).
2. **Chat Completions остаётся провайдером `openai_compatible`.** Это нынешний `OpenAILLM`
   для OpenAI-совместимых API. Ключ и адрес: `OPENAI_COMPATIBLE_API_KEY` и
   `OPENAI_COMPATIBLE_BASE_URL`. `OPENAI_API_KEY` и `OPENAI_BASE_URL` относятся к `openai`
   (Responses). В контракте `ModelConfig.provider` появляется значение `openai_compatible`
   (по ADR-0005: схема → `make contracts`).
3. **Порт `LLMClient` получает непрозрачные данные провайдера.** У `LLMResponse` и
   `AssistantMessage` появляется поле `provider_items: tuple[dict[str, Any], ...] = ()`. Адаптер
   кладёт туда то, что провайдер просит вернуть: reasoning items Responses API. Цикл агента
   передаёт их обратно без изменений вместе с `response.as_message()`. Адаптер, который поле
   не понимает, игнорирует его. Знание о содержимом живёт только в адаптере (как в ADR-0009).
4. **`provider_items` живут в пределах хода.** В БД они не пишутся. История прошлых ходов, как
   и сейчас, восстанавливается из текста сообщений, и рассуждения прошлых ходов модель не видит.
   Responses API это допускает (`reasoning.context: current_turn`).
5. **Сэмплинг** — по ADR-0009. Новый адаптер отбрасывает `temperature` так же адаптивно, как
   `OpenAILLM` (400 с `param: "temperature"` → один повтор без неё, модель запоминается), и
   перечисляет передаваемые параметры в docstring.
6. **demo-beauty** остаётся на `provider: openai`, `gpt-6.1-sol`, теперь через Responses API.
   `update_dialog_state` возвращается в `tools.builtin`.

## Альтернативы
- **Сменить модель demo-beauty и не трогать адаптеры.** Модели Anthropic (нужен отдельный
  ключ) или GPT-6 Sol/Luna с `reasoning_effort: "none"` через Chat Completions. Тогда в
  `ModelConfig` нужно поле `reasoning_effort`, а модель отвечает без рассуждений и хуже на
  многошаговых подборах. Главное — проблема остаётся у платформы: ни один тенант не сможет
  поставить GPT-6 Astra или GPT-6.1 Sol. Годится как временная мера, но не как решение.
- **Responses API с хранением на стороне OpenAI (`store: true` + `previous_response_id`).**
  Вход короче, reasoning items передавать не нужно. Но клиент перестаёт быть без состояния:
  контекст хода живёт у провайдера, и переключиться на fallback-модель другого провайдера
  посреди хода (P7-01) нельзя. Диалоги всех тенантов хранились бы у OpenAI (по умолчанию 30
  дней), а это решение о данных клиентов, которое не нужно принимать ради одного API.
  Отказались.
- **Один провайдер `openai` целиком на Responses API, без Chat Completions.** Адаптер один,
  но совместимые API (RouterAI, OpenRouter, локальные серверы) в основном поддерживают только
  Chat Completions, и путь через прокси, проверенный в P2-08, пропадает. Выбирать API по
  `base_url` неявно и хрупко. Выбирать отдельным полем `ModelConfig.api` значит добавить в
  схему поле, которое имеет смысл только для одного провайдера, то есть то ветвление по
  провайдеру в схеме, от которого отказался ADR-0009. Отказались.

## Последствия
- Уточняет ADR-0002: порт остаётся одним для всех провайдеров, но может переносить
  непрозрачные данные конкретного провайдера между шагами хода. ADR-0009 не меняется: новый
  адаптер следует ему. ADR-0005 соблюдается: `provider` меняется через схему. Ни один принятый
  ADR не заменяется.
- `provider` в AgentConfig означает протокол API, а не производителя модели. Например,
  `openai_compatible` с моделью `anthropic/…` через прокси. В описании поля в схеме это нужно
  сказать явно.
- Меняется смысл `OPENAI_BASE_URL`: прокси теперь настраиваются через
  `OPENAI_COMPATIBLE_BASE_URL`. Нужно обновить `.env.example`. Конфиги с `provider: openai`,
  которые ходили через прокси, придётся перевести на `openai_compatible`.
- Общий контрактный тест `LLMClient` (P2-07) должен проходить на трёх адаптерах. Новый тест —
  шаг с вызовом инструмента, после которого reasoning items возвращаются во входе следующего
  шага. Плюс live-тест (маркер `live`) на Responses API с инструментом.
- `FakeLLM` должен уметь отдавать `provider_items`, а цикл агента — тестироваться на то, что
  они доходят до следующего запроса без изменений.
- Задача P2-11 разбивается на реализацию: адаптер Responses API; `openai_compatible` в
  контракте и `LLMClients`; `provider_items` в порте и цикле; возврат `update_dialog_state` в
  demo-beauty и живая проверка хода с вызовом инструмента.
