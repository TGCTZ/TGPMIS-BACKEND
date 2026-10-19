"""Shared service helpers available to every app."""

import re

from django.db import IntegrityError, transaction

_ORDER_REFERENCE_RE = re.compile(r"^ORD-(\d{4})-(\d+)$")
_DISPLAY_REFERENCE_RE = re.compile(
    r"^(?P<prefix>[A-Z]+)-(?P<year>\d{4})-(?P<sequence>\d+)(?P<label>-[A-Za-z0-9]+)?$"
)


def financial_year(when=None) -> tuple[int, int]:
    """The Tanzanian financial year containing ``when``, as ``(start, end)``.

    Runs July to June, so a date in August 2026 falls in 2026/2027 and a date in
    March 2027 falls in the same one. Ported from the legacy system's
    ``reception_app/lib.py``, which is what has been printing these year pairs on
    certificates to date.

    Args:
        when: The moment to place. Defaults to now.
    """
    from django.utils import timezone

    moment = when or timezone.now()
    if moment.month >= 7:
        return moment.year, moment.year + 1
    return moment.year - 1, moment.year


def _highest_order_sequence(order_model, year_code: str) -> int:
    """Read the largest stored sequence for a year, including deleted orders."""
    stem = f"ORD-{year_code}-"
    highest = 0
    for reference in order_model.all_objects.filter(
        reference_number__startswith=stem
    ).values_list("reference_number", flat=True):
        match = _ORDER_REFERENCE_RE.fullmatch(reference)
        if match is not None and match.group(1) == year_code:
            highest = max(highest, int(match.group(2)))
    return highest


@transaction.atomic
def generate_order_reference_number() -> str:
    """Allocate the next order number, e.g. ``ORD-2627-1``.

    The counter is initialized from all stored orders in the current financial
    year, including soft-deleted records, so historical identifiers are never
    reused. A row lock serializes later allocations for that year. The unique
    order-reference constraint remains the final safeguard against duplicates.

    A savepoint handles two requests creating the first counter row for a new
    financial year at the same time. After the row exists, each allocator locks
    it before incrementing.
    """
    from apps.core.models import OrderSequence
    from apps.orders.models import Order

    start, end = financial_year()
    year_code = f"{start % 100:02d}{end % 100:02d}"
    sequence = (
        OrderSequence.objects.select_for_update().filter(financial_year=year_code).first()
    )
    if sequence is None:
        try:
            with transaction.atomic():
                sequence = OrderSequence.objects.create(
                    financial_year=year_code,
                    last_sequence=_highest_order_sequence(Order, year_code),
                )
        except IntegrityError:
            # Another transaction inserted the unique year row while this one
            # was scanning. The savepoint keeps this transaction usable.
            sequence = OrderSequence.objects.select_for_update().get(
                financial_year=year_code
            )

    next_value = int(sequence.last_sequence) + 1
    reference = f"ORD-{year_code}-{next_value}"
    # Imported or manually restored orders can appear after a counter was
    # initialized. Keep their identifiers reserved as well.
    while Order.all_objects.filter(reference_number=reference).exists():
        next_value += 1
        reference = f"ORD-{year_code}-{next_value}"
    sequence.last_sequence = str(next_value)
    sequence.save(update_fields=["last_sequence"])
    return reference


def reference_number_for_order(
    order, prefix: str, *, stone_label: str | None = None
) -> str:
    """Build a related document reference from its order's year and sequence.

    Order numbers are the sequence source of truth. Bills share the order number
    at order level; findings and certificates add the stone label so each
    per-stone record remains distinguishable.
    """
    match = _ORDER_REFERENCE_RE.fullmatch(order.reference_number)
    if match is None:
        raise ValueError(
            f"Order reference {order.reference_number!r} does not contain a valid "
            "year and sequence."
        )

    year, sequence = match.groups()
    reference = f"{prefix}-{year}-{int(sequence)}"
    return f"{reference}-{stone_label}" if stone_label else reference


def format_reference_number(value: str | None) -> str | None:
    """Format a stored reference's financial year for people, not integrations.

    Stored identifiers deliberately remain slash-free so they are safe as
    GePG values, filenames, and verification URL path segments.
    """
    if value is None:
        return None
    match = _DISPLAY_REFERENCE_RE.fullmatch(value)
    if match is None:
        return value

    prefix = match.group("prefix")
    year = match.group("year")
    sequence = match.group("sequence")
    label = match.group("label") or ""
    return f"{prefix}-{year[:2]}/{year[2:]}-{sequence}{label}"
