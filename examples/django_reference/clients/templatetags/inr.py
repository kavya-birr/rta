"""Indian-numbering-system (lakhs/crores) template filters.

Registered as:
  {% load inr %}
  {{ value|inr }}              → 2,87,97,699.66
  {{ value|inr:0 }}             → 2,87,97,700
  {{ value|inr_signed }}        → -2,87,97,699.66 (keeps sign)
"""
from __future__ import annotations

from django import template

register = template.Library()


def _format_indian(amount: float, decimals: int = 2) -> str:
    """Format a number with Indian comma grouping (lakhs/crores).

    Examples:
        12345.67   → 12,345.67
        1234567.89 → 12,34,567.89
        28797699.66 → 2,87,97,699.66
    """
    if amount is None:
        return "—"
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return "—"

    sign = "-" if amount < 0 else ""
    amount = abs(amount)

    # Split integer and fractional parts with the requested decimals
    if decimals > 0:
        int_str, frac_str = f"{amount:.{decimals}f}".split(".")
    else:
        int_str = f"{round(amount):.0f}"
        frac_str = ""

    # Indian format: last 3 digits, then groups of 2 from the right
    if len(int_str) > 3:
        head, tail = int_str[:-3], int_str[-3:]
        # Insert commas every 2 chars in head, reading from the right
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join(parts) + "," + tail
    else:
        grouped = int_str

    if frac_str:
        return f"{sign}{grouped}.{frac_str}"
    return f"{sign}{grouped}"


@register.filter(name="inr")
def inr(value, decimals=2):
    """Format with Indian comma grouping, default 2 decimals."""
    try:
        return _format_indian(value, int(decimals))
    except (TypeError, ValueError):
        return "—"


@register.filter(name="inr0")
def inr0(value):
    """Indian-grouped integer (no decimals)."""
    return _format_indian(value, 0)


@register.filter(name="inr_abs")
def inr_abs(value, decimals=2):
    """Absolute-value version (sign stripped — caller may apply its own +/−)."""
    if value is None:
        return "—"
    try:
        return _format_indian(abs(float(value)), int(decimals))
    except (TypeError, ValueError):
        return "—"


@register.filter(name="get_item")
def get_item(mapping, key):
    """Dict / mapping lookup — Django templates lack dict[key] syntax."""
    if mapping is None:
        return None
    try:
        return mapping.get(key)
    except AttributeError:
        try:
            return mapping[key]
        except (KeyError, IndexError, TypeError):
            return None
