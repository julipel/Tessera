"""Маскирование персональных данных в трейсах перед отправкой во внешний сервис (P7-07,
ADR-0032).

По шаблонам — e-mail, телефоны, номера карт, Telegram-ники и ссылки `t.me/…` в любых строках,
телефоны и карты, записанные числом (`int` из 10-19 цифр); по структуре — значения
отправленных форм (`values`), полей заявки (`fields`), факты о клиенте (`facts`) и значения
слотов (`slots`: аргументы `update_dialog_state` и состояние диалога в Runtime-слое промпта)
целиком: имена и адреса шаблонами не поймать. Названия слотов (сценарий, слот) остаются —
видно, что заполнено. JSON в строке (объект или массив) разбирается и маскируется так же.
Имена в свободном тексте сообщений не маскируются — известное ограничение. `has_contact` —
те же шаблоны для проверки факта перед записью в состояние диалога (P7-07b).

Числа: серия цифр с разделителями (пробелы, в том числе неразрывный и тонкие, точки, скобки,
дефисы и тире) режется на части по разделителям с пробелом (точки, дефисы и скобки не режут:
дата `15.10.2026` — одна часть). Буквы и цифры перед серией и после неё (UUID, артикулы) —
только латиница: телефон, слитый с русским словом (`звони89991112233`), маскируется.
Из подряд идущих частей выбираются телефоны (10-15 цифр) и карты (13-19 цифр с верной
контрольной суммой Луна) так, чтобы под маской было больше цифр:
`89991112233 15.10.2026` → `[phone] 15.10.2026`, два телефона подряд — два `[phone]`.
16 цифр четырьмя группами по 4 — карта и с несходящейся суммой (опечатка в номере):
`1234 5678 9012 3456` → `[card]`.

Все шаблоны линейны по длине текста: маскирование идёт синхронно в корутине хода.
"""

import json
import re
from typing import Any

HIDDEN = "[скрыто]"
# Ключи, значения под которыми — данные клиента: форма (UserInput form_submit), поля заявки
# (create_lead), факты о клиенте в свободной форме (DialogState.facts).
_FORM_KEYS = frozenset({"values", "fields", "facts"})
# Слоты сценариев: `{слот: значение}` или `{сценарий: {слот: значение}}` — среди них адреса,
# имена получателей (O-10); скрываются значения, названия остаются.
_SLOTS_KEY = "slots"

# Левая граница: без неё поиск начинался бы с каждой буквы длинной серии — квадратичное время.
_EMAIL = re.compile(r"(?<![\w.+\-])[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_HANDLE = re.compile(r"(?<![\w@.])@[a-zA-Z][a-zA-Z0-9_]{3,31}\b")
_TELEGRAM_LINK = re.compile(
    r"(?<![\w./])(?:https?://)?(?:www\.)?(?:t|telegram)\.me/[A-Za-z0-9_+/]{1,64}",
    re.IGNORECASE,
)
# Пробелы внутри номера (обычный, неразрывный, цифровой и тонкие) и прочие разделители:
# точки, скобки, дефисы (в том числе неразрывный). Тире — не разделитель: им пишут
# диапазоны («бюджет 10 000 — 15 000 ₽» через en dash без пробелов — не телефон).
_SPACES = " \u00a0\u2007\u2009\u202f"
_SEPARATORS = _SPACES + r"\-()." + "\u2010\u2011"
# Серия цифр с разделителями, целиком (посессивно — без бэктрекинга); телефоны и карты в ней
# ищет `_digits`. Перед серией латинская буква, цифра или дефис — не наш случай (UUID,
# артикулы), после серии — то же для её последней части.
_DIGITS = re.compile(rf"(?<![A-Za-z0-9_\-+])\+?\d(?:[{_SEPARATORS}]{{0,2}}\d)*+")
# Граница частей серии — разделитель с пробелом.
_DIGIT_SPLIT = re.compile(rf"[\-().\u2010\u2011]*[{_SPACES}][{_SEPARATORS}]*")
# Карта, записанная четырьмя группами по 4 цифры: маскируется и с несходящейся суммой Луна.
_CARD_GROUPS = re.compile(rf"\d{{4}}(?:[{_SEPARATORS}]{{1,2}}\d{{4}}){{3}}")
_NOT_DIGIT = re.compile(r"\D")
_MAX_SEGMENT = 19
# Факты и слоты в JSON внутри текста: состояние диалога в Runtime-слое системного промпта.
_STATE_PARTS = re.compile(r'"(facts|slots)":\s*(?=[\[{])')
_DECODER = json.JSONDecoder()
# Неудачных разборов JSON состояния на текст, после — остаток скрыт: каждая ошибка разбора
# стоит O(позиции) (JSONDecodeError считает номер строки с начала текста).
_MAX_STATE_FAILURES = 16
# Отправка формы в истории для модели (chat `_user_text`).
_FORM_SUBMIT = re.compile(r"(\[Отправлена форма [\w\-]+: )(\{.*?\})(\])", re.DOTALL)


def mask_pii(data: Any) -> Any:
    """Копия `data` (str, int, dict, list, tuple) с замаскированными персональными данными;
    тип контейнера сохраняется. Телефон или карта числом становится строкой-меткой."""
    if isinstance(data, str):
        return _mask_text(data)
    if isinstance(data, dict):
        return {key: _mask_item(key, value) for key, value in data.items()}
    if isinstance(data, list):
        return [mask_pii(item) for item in data]
    if isinstance(data, tuple):
        return tuple(mask_pii(item) for item in data)
    if isinstance(data, int) and not isinstance(data, bool) and data > 0:
        digits = str(data)
        if 10 <= len(digits) <= _MAX_SEGMENT and (masked := _mask_contacts(digits)) != digits:
            return masked
    return data


def _mask_item(key: Any, value: Any) -> Any:
    if key in _FORM_KEYS:
        return _hide_values(value)
    if key == _SLOTS_KEY:
        return _hide_leaves(value)
    return mask_pii(value)


def has_contact(text: str) -> bool:
    """Есть ли в строке e-mail, телефон, номер карты или Telegram-ник (шаблоны `mask_pii`)."""
    return _mask_contacts(text) != text


def _hide_values(value: Any) -> Any:
    if isinstance(value, dict):
        return dict.fromkeys(value, HIDDEN)
    if isinstance(value, list):
        return [HIDDEN] * len(value)
    return HIDDEN if value not in (None, "", [], {}) else value


def _hide_leaves(value: Any) -> Any:
    """Значения слотов скрыты, названия (ключи на любой глубине) остаются."""
    if isinstance(value, dict):
        return {key: _hide_leaves(item) for key, item in value.items()}
    return _hide_values(value)


def _mask_text(text: str) -> str:
    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        # JSON в строке: аргументы вызова инструмента, результат инструмента для модели.
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict | list):
                return json.dumps(mask_pii(parsed), ensure_ascii=False)
        except ValueError:
            pass
        except RecursionError:
            # Вложенность глубже, чем разберёт json: ключи форм в ней не найти — целиком.
            return HIDDEN
    text = _hide_state_parts(text)
    text = _FORM_SUBMIT.sub(_form_submit, text)
    return _mask_contacts(text)


def _mask_contacts(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _TELEGRAM_LINK.sub("[handle]", text)
    text = _HANDLE.sub("[handle]", text)
    return _DIGITS.sub(lambda match: _digits(match, text), text)


def _hide_state_parts(text: str) -> str:
    """Список после `"facts":` и объект после `"slots":` в тексте — замаскированными (конец —
    по разбору JSON, а не по первой `]` или `}`: она может быть внутри значения).

    Битый или обрезанный JSON скрывается до места, где разбор упал (с запасом: вложенные
    факты и слоты не утекают), и не разбирается заново — иначе вложенные `"slots": {…` без
    конца разбирались бы каждый до конца текста. После `_MAX_STATE_FAILURES` неудач и при
    слишком глубокой вложенности (RecursionError) остаток текста скрыт целиком. Время —
    линейное по длине текста."""
    parts: list[str] = []
    position = 0
    failures = 0
    for match in _STATE_PARTS.finditer(text):
        # Начало ключа могло уйти в скрытый кусок битого JSON, а значение после него — нет.
        if match.end() <= position:
            continue
        try:
            value, end = _DECODER.raw_decode(text, match.end())
        except json.JSONDecodeError as e:
            failures += 1
            end = len(text) if failures > _MAX_STATE_FAILURES else max(e.pos, match.end())
            parts += [text[position : match.end()], json.dumps(HIDDEN, ensure_ascii=False)]
            position = end
            continue
        except RecursionError:
            parts += [text[position : match.end()], json.dumps(HIDDEN, ensure_ascii=False)]
            position = len(text)
            break
        if match.group(1) == _SLOTS_KEY:
            hidden = _hide_leaves(value) if isinstance(value, dict) else HIDDEN
        else:
            hidden = _hide_values(value) if isinstance(value, list) else HIDDEN
        parts += [text[position : match.end()], json.dumps(hidden, ensure_ascii=False)]
        position = end
    return "".join(parts) + text[position:] if parts else text


def _form_submit(match: re.Match[str]) -> str:
    try:
        values = json.loads(match.group(2))
    except ValueError:
        return f"{match.group(1)}{HIDDEN}{match.group(3)}"
    return f"{match.group(1)}{json.dumps(_hide_values(values), ensure_ascii=False)}{match.group(3)}"


def _digits(match: re.Match[str], text: str) -> str:
    """Серия цифр с телефонами и картами под маской (см. докстринг модуля)."""
    run = match.group(0)
    bounds: list[tuple[int, int]] = []
    start = 0
    for separator in _DIGIT_SPLIT.finditer(run):
        bounds.append((start, separator.start()))
        start = separator.end()
    bounds.append((start, len(run)))
    parts = [_NOT_DIGIT.sub("", run[a:b]) for a, b in bounds]
    after = text[match.end() : match.end() + 1]
    glued = bool(after) and ((after.isascii() and after.isalnum()) or after in "_-")
    segments = _best_segments(run, bounds, parts, len(parts) - 1 if glued else len(parts))
    if not segments:
        return run
    pieces: list[str] = []
    position = 0
    for first, last in segments:
        digits = "".join(parts[first : last + 1])
        raw = run[bounds[first][0] : bounds[last][1]]
        card = len(digits) >= 13 and (_luhn(digits) or _CARD_GROUPS.fullmatch(raw) is not None)
        label = "[card]" if card else "[phone]"
        pieces += [run[position : bounds[first][0]], label]
        position = bounds[last][1]
    return "".join(pieces) + run[position:]


def _best_segments(
    run: str, bounds: list[tuple[int, int]], parts: list[str], usable: int
) -> list[tuple[int, int]]:
    """Непересекающиеся отрезки частей `parts[:usable]` — телефоны и карты — с наибольшим
    числом цифр под маской; при равенстве — меньше отрезков, затем длиннее первый.
    Отрезок не длиннее 19 цифр (и не длиннее 57 символов серии: между цифрами не больше
    двух разделителей), поэтому время — O(число частей * 19)."""
    starts = [0]
    for part in parts:
        starts.append(starts[-1] + len(part))
    luhn = _LuhnPrefix("".join(parts))
    count = len(parts)
    # best[i] — (цифр под маской, -отрезков) для parts[i:] и последняя часть первого отрезка.
    best: list[tuple[tuple[int, int], int | None]] = [((0, 0), None)] * (count + 1)
    for i in range(count - 1, -1, -1):
        score, first_end = best[i + 1][0], None
        for j in range(i, usable):
            length = starts[j + 1] - starts[i]
            if length > _MAX_SEGMENT:
                break
            if 10 <= length <= 15 or (
                length >= 16
                and (
                    luhn.valid(starts[i], starts[j + 1])
                    or _CARD_GROUPS.fullmatch(run, bounds[i][0], bounds[j][1]) is not None
                )
            ):
                rest = best[j + 1][0]
                candidate = (rest[0] + length, rest[1] - 1)
                if candidate >= score:
                    score, first_end = candidate, j
        best[i] = (score, first_end)
    segments: list[tuple[int, int]] = []
    i = 0
    while i < count:
        last = best[i][1]
        if last is None:
            i += 1
            continue
        segments.append((i, last))
        i = last + 1
    return segments


class _LuhnPrefix:
    """Контрольная сумма Луна любой подстроки цифр за O(1) — по префиксным суммам."""

    def __init__(self, digits: str) -> None:
        # sums[k][p] — сумма первых p цифр, где удвоены цифры на позициях чётности k.
        self.sums: tuple[list[int], list[int]] = ([0], [0])
        for position, char in enumerate(digits):
            plain = int(char)
            doubled = plain * 2 - 9 if plain > 4 else plain * 2
            for parity, sums in enumerate(self.sums):
                sums.append(sums[-1] + (doubled if position % 2 == parity else plain))

    def valid(self, start: int, end: int) -> bool:
        # Удваивается каждая вторая цифра справа: позиции той же чётности, что end - 2.
        sums = self.sums[end % 2]
        return (sums[end] - sums[start]) % 10 == 0


def _luhn(digits: str) -> bool:
    return _LuhnPrefix(digits).valid(0, len(digits))
