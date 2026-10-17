"""Доменные ошибки модуля leads."""


class InvalidCursorError(ValueError):
    """Курсор страницы не разбирается — подделан или от другого списка."""


class TooManyLeadsError(ValueError):
    """Выгрузка больше MAX_EXPORT заявок — нужно сузить период (ADR-0045)."""


class InvalidPeriodError(ValueError):
    """Начало периода выгрузки не раньше конца."""
