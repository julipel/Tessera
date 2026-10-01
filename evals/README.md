# Эвалы качества диалога

Каждый файл `dialogs/*.yaml` — эталонный диалог. Раннер (`make eval`, задача P3-01) проигрывает
реплики пользователя через агента и проверяет ожидания на каждом ходе. Отчёты — в `reports/`
(в .gitignore).

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
