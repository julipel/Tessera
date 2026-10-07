"""Маскирование персональных данных в трейсах перед отправкой во внешний сервис (P7-07,
ADR-0032).

По шаблонам — e-mail, телефоны, номера карт, Telegram-ники в любых строках; по структуре —
значения отправленных форм (`values`), полей заявки (`fields`) и факты о клиенте (`facts`:
аргументы `update_dialog_state` и состояние диалога в Runtime-слое промпта) целиком: имена
и адреса шаблонами не поймать. Имена в свободном тексте сообщений не маскируются — известное
ограничение.
"""

import json
import re
from typing import Any

HIDDEN = "[скрыто]"
# Ключи, значения под которыми — данные клиента: форма (UserInput form_submit), поля заявки
# (create_lead), факты о клиенте в свободной форме (DialogState.facts).
_FORM_KEYS = frozenset({"values", "fields", "facts"})

_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_HANDLE = re.compile(r"(?<![\w@.])@[a-zA-Z][a-zA-Z0-9_]{3,31}\b")
# Цифры с разделителями; длина проверяется отдельно: телефон — от 10 до 15 цифр, карта —
# от 13 до 19 и контрольная сумма Луна. Рядом буква, цифра или дефис — не наш случай
# (UUID, артикулы).
_DIGITS = re.compile(r"(?<![\w\-+])\+?\d(?:[ \-().\u00a0]{0,2}\d){9,18}(?![\w\-])")
# Факты в JSON внутри текста: состояние диалога в Runtime-слое системного промпта.
_FACTS = re.compile(r'"facts":\s*(?=\[)')
_DECODER = json.JSONDecoder()
# Отправка формы в истории для модели (chat `_user_text`).
_FORM_SUBMIT = re.compile(r"(\[Отправлена форма [\w\-]+: )(\{.*?\})(\])", re.DOTALL)


def mask_pii(data: Any) -> Any:
    """Копия `data` (str, dict, list) с замаскированными персональными данными."""
    if isinstance(data, str):
        return _mask_text(data)
    if isinstance(data, dict):
        return {
            key: _hide_values(value) if key in _FORM_KEYS else mask_pii(value)
            for key, value in data.items()
        }
    if isinstance(data, list | tuple):
        return [mask_pii(item) for item in data]
    return data


def _hide_values(value: Any) -> Any:
    if isinstance(value, dict):
        return dict.fromkeys(value, HIDDEN)
    if isinstance(value, list):
        return [HIDDEN] * len(value)
    return HIDDEN if value not in (None, "", [], {}) else value


def _mask_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        # JSON в строке: аргументы вызова инструмента, результат инструмента для модели.
        try:
            parsed = json.loads(stripped)
        except ValueError:
            pass
        else:
            if isinstance(parsed, dict):
                return json.dumps(mask_pii(parsed), ensure_ascii=False)
    text = _hide_facts(text)
    text = _FORM_SUBMIT.sub(_form_submit, text)
    text = _EMAIL.sub("[email]", text)
    text = _HANDLE.sub("[handle]", text)
    return _DIGITS.sub(_digits, text)


def _hide_facts(text: str) -> str:
    """Список после `"facts":` в тексте — замаскированным целиком (конец списка — по разбору
    JSON, а не по первой `]`: она может быть внутри факта)."""
    parts: list[str] = []
    position = 0
    for match in _FACTS.finditer(text):
        if match.start() < position:
            continue
        try:
            facts, end = _DECODER.raw_decode(text, match.end())
        except ValueError:
            continue
        hidden = _hide_values(facts) if isinstance(facts, list) else HIDDEN
        parts += [text[position : match.end()], json.dumps(hidden, ensure_ascii=False)]
        position = end
    return "".join(parts) + text[position:] if parts else text


def _form_submit(match: re.Match[str]) -> str:
    try:
        values = json.loads(match.group(2))
    except ValueError:
        return f"{match.group(1)}{HIDDEN}{match.group(3)}"
    return f"{match.group(1)}{json.dumps(_hide_values(values), ensure_ascii=False)}{match.group(3)}"


def _digits(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn(digits):
        return "[card]"
    if 10 <= len(digits) <= 15:
        return "[phone]"
    return match.group(0)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0
