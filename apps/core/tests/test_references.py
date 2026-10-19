"""The one reference shape every document in the system carries."""

import re
from datetime import datetime

import pytest

from apps.core import services
from apps.core.services import (
    financial_year,
    format_reference_number,
    generate_order_reference_number,
    reference_number_for_order,
)
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


def _stem() -> str:
    """This financial year as it appears in a reference, e.g. ``2627``."""
    start, end = financial_year()
    return f"{start % 100:02d}{end % 100:02d}"


def test_financial_year_runs_july_to_june():
    """The lab reports on a July-June year, so August and March share one."""
    assert financial_year(datetime(2026, 8, 1)) == (2026, 2027)
    assert financial_year(datetime(2027, 3, 1)) == (2026, 2027)

    # The boundaries themselves, which is where an off-by-one would hide.
    assert financial_year(datetime(2026, 6, 30)) == (2025, 2026)
    assert financial_year(datetime(2026, 7, 1)) == (2026, 2027)


def test_order_references_use_an_unpadded_natural_sequence():
    """Orders use PREFIX-<fy>-N without a fixed sequence width.

    Related document types keep the same year and sequence while adding their
    prefix and, for per-stone papers, the stone's existing label.
    """
    stem = _stem()
    number = generate_order_reference_number()
    assert re.fullmatch(r"ORD-\d{4}-\d+", number)
    assert number == f"ORD-{stem}-1"

    # No slashes anywhere: these end up in filenames and URL path segments.
    assert "/" not in number


def test_the_sequence_counts_up_within_a_financial_year():
    """Each new document takes the next number, soft-deleted ones included."""
    stem = _stem()
    OrderFactory(reference_number=f"ORD-{stem}-00007")

    assert generate_order_reference_number() == f"ORD-{stem}-8"

    # A soft-deleted order still holds its number: `all_objects` is scanned, so
    # a number is never handed out twice.
    OrderFactory(reference_number=f"ORD-{stem}-00008").delete()
    assert generate_order_reference_number() == f"ORD-{stem}-9"


def test_the_sequence_runs_past_the_old_five_digit_ceiling():
    """The sequence remains numeric after the old 99,999 limit."""
    stem = _stem()
    OrderFactory(reference_number=f"ORD-{stem}-099999")

    assert generate_order_reference_number() == f"ORD-{stem}-100000"


def test_the_sequence_restarts_each_financial_year(monkeypatch):
    """The year pair says when, the sequence says how many since July."""
    OrderFactory(reference_number="ORD-2627-00042")

    monkeypatch.setattr(services, "financial_year", lambda when=None: (2026, 2027))
    assert generate_order_reference_number() == "ORD-2627-43"

    # July arrives: a new year pair is a new stem, so the scan finds nothing to
    # continue from and the count starts over rather than running on at 00044.
    monkeypatch.setattr(services, "financial_year", lambda when=None: (2027, 2028))
    assert generate_order_reference_number() == "ORD-2728-1"


def test_related_references_reuse_the_order_sequence_and_stone_label():
    """Bills share order sequence; per-stone records add their label."""
    order = OrderFactory(reference_number="ORD-2627-00042")

    assert reference_number_for_order(order, "BILL") == "BILL-2627-42"
    assert reference_number_for_order(order, "TGC", stone_label="A") == "TGC-2627-42-A"
    assert (
        reference_number_for_order(order, "CERT", stone_label="1") == "CERT-2627-42-1"
    )


def test_display_format_adds_slash_only_to_the_financial_year():
    """The slash belongs to the human-readable form, not stored IDs."""
    assert format_reference_number("ORD-2627-00042") == "ORD-26/27-00042"
    assert format_reference_number("TGC-2627-00042-A") == "TGC-26/27-00042-A"
    assert format_reference_number("TGC-2627-42-1") == "TGC-26/27-42-1"
    assert format_reference_number("CERT-legacy") == "CERT-legacy"


def test_the_year_pair_is_written_as_four_digits(monkeypatch):
    """2026/2027 reads `2627` - short enough to say over a counter."""
    monkeypatch.setattr(services, "financial_year", lambda when=None: (2026, 2027))
    assert generate_order_reference_number() == "ORD-2627-1"

    # A year ending in 0x keeps its leading zero, or the segment would shrink to
    # three digits and stop sorting against its neighbours.
    monkeypatch.setattr(services, "financial_year", lambda when=None: (2029, 2030))
    assert generate_order_reference_number() == "ORD-2930-1"
    monkeypatch.setattr(services, "financial_year", lambda when=None: (2099, 2100))
    assert generate_order_reference_number() == "ORD-9900-1"
