"""Числа каталога для модели и компонентов."""

from decimal import Decimal


def plain_number(value: Decimal) -> int | float:
    """Decimal из БД → JSON-число: целое без «.0», иначе float."""
    return int(value) if value == value.to_integral_value() else float(value)
