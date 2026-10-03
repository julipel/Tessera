# Эвалы качества диалога

Каждый файл `dialogs/*.yaml` — эталонный диалог. Раннер (`make eval`, задача P3-01) проигрывает
реплики пользователя через агента и проверяет ожидания на каждом ходе. Отчёты — в `reports/`
(в .gitignore).

## Запуск

```bash
make eval                 # все диалоги
make eval f=beauty_14     # подстрока id или тег (f=lead)
cd apps/api && uv run python -m evals.run --concurrency 2 -v   # флаги: --help
```

Раннер (`apps/api/evals/`, ADR-0011) гоняет агента in-process: тот же `LoopTurnAgent`, что и
chat API, конфиг тенанта — из `config/tenants/<tenant>.yaml` (seed и Docker не нужны), ключи
провайдера — из `.env`. Диалоги идут параллельно, ходы внутри диалога — по очереди; ошибка
агента на ходе помечает ход ⚠️, остальные ходы диалога пропускаются.

Результат — `reports/<timestamp>.md` (читать) и `reports/<timestamp>.json` (сравнение
прогонов, P3-02), сводка — в консоль. Код выхода 1, если есть проваленные или упавшие
диалоги.

### LLM-судья

`clarifies`, `max_questions` и `judge` оценивает LLM-судья (`apps/api/evals/judge.py`): один
вызов на ход, только если в `expect` есть эти проверки. Судья видит предыдущие ходы, текущую
реплику, ответ, вызванные инструменты, компоненты и слоты; отвечает JSON
`{questions, clarifies, rubric: {pass, reason}}`, пояснение попадает в отчёт. Сбой модели или
неразборчивый ответ — ⚠️ `error` у проверок судьи этого хода, прогон продолжается.

- `make eval` судит моделью `EVAL_JUDGE_PROVIDER`/`EVAL_JUDGE_MODEL` из Makefile (по умолчанию
  `openai/gpt-6.1-sol`, агент demo-beauty — `gpt-6-luna`): модель не оценивает сама себя;
  `python -m evals.run` без `--judge-provider` / `--judge-model` судит `model.primary` тенанта;
- `--no-judge` — дешёвый прогон без судьи (его проверки `skipped`);
- версия промпта судьи (`JUDGE_PROMPT_VERSION`) пишется в отчёт: при её смене прогоны
  сравнивать напрямую нельзя.

## Формат

```yaml
id: gift_selection_with_budget
tenant: demo
scenario: product_selection
tags: [clarification, catalog]
turns:
  - user: "Хочу подарок маме"
    expect:
      clarifies: true                  # агент должен задать уточняющий вопрос
      max_questions: 1
      tools_not_called: [show_entities]
  - user: "Любит готовить, бюджет до 1000 крон"
    expect:
      tools_called: [search_catalog]
      components: [product_carousel]
      state_contains: { budget: 1000 }
      must_not_contain: ["не могу помочь"]
  - user: "А что-нибудь подешевле?"
    expect:
      tools_called: [search_catalog]
      clarifies: false                 # контекст уже есть — не переспрашивать
      judge: "Учитывает, что подарок для мамы, которая любит готовить; цены ниже предыдущих"
```

Проверки:
- `clarifies`, `max_questions` — определяются LLM-судьёй по ответу.
- `tools_called` / `tools_not_called` — по фактическим вызовам (порядок не важен).
- `components` — типы отправленных UI-компонентов.
- `state_contains` — подмножество DialogState после хода.
- `must_contain` / `must_not_contain` — подстроки (регистр игнорируется).
- `judge` — свободная рубрика для LLM-судьи, оценка pass/fail + пояснение.

Для списков в `state_contains` проверяется вхождение элементов, а не равенство
(`concerns: [dullness]` — в слоте есть `dullness`, могут быть и другие).

## Ввод не текстом

Вместо `user:` ход может содержать `input:` в формате UserInput из docs/contracts.md —
для нажатий кнопок и отправки форм:

```yaml
- input:
    type: form_submit
    form_id: consultation
    values: { name: "Анна", contact: "@anna_test" }
  expect:
    components: [confirm]
- input:
    type: action
    action_id: confirm
    payload: {}
  expect:
    tools_called: [create_lead]
```

Раннер подставляет реальные `form_id` / `confirm_id` из предыдущего хода, если в эталоне
указан ключ формы или служебное `action_id: confirm`.

## Что покрыть в P0-02
Простой вопрос по знаниям; вопрос, на который в базе нет ответа (агент честно говорит);
подбор с уточнениями; уточнение, когда всё уже сказано (не должен переспрашивать);
смена темы посреди диалога; возврат к ранее показанным товарам («а второй вариант?»);
запрос вне компетенции; попытка заставить агента игнорировать инструкции;
действие с подтверждением (заявка).
