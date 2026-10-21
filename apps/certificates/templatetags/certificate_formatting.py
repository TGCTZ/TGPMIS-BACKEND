"""Formatting helpers for values printed on certificate documents."""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()

_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


@register.filter
def format_certificate_measurement(value: object) -> str:
    """Format numeric readings to two places while preserving range notation."""
    if value is None:
        return ""

    def format_number(match: re.Match[str]) -> str:
        try:
            rounded = Decimal(match.group()).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        except InvalidOperation:
            return match.group()
        return f"{rounded:.2f}"

    return _NUMBER.sub(format_number, str(value))
