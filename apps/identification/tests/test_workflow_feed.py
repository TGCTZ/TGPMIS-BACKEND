"""Findings feed represents a draft report once instead of duplicating its stone."""

from decimal import Decimal

import pytest

from apps.billing.dev import simulate_payment
from apps.billing.services import generate_bill_for_order
from apps.gems.tests.factories import StoneTypeFactory
from apps.identification.models import IdentificationReport
from apps.identification.services import create_report
from apps.orders.services import add_stone
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


def test_findings_feed_does_not_duplicate_a_draft_report_as_a_stone(
    settings, admin_user, auth_client
):
    """A draft stays one actionable report row instead of a duplicate stone."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=3)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("50000.00")))
    bare_stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("50000.00")))
    finalized_stone = add_stone(
        order, stone_type=StoneTypeFactory(price=Decimal("50000.00"))
    )
    simulate_payment(generate_bill_for_order(order))
    report = create_report(stone=stone, user=admin_user, conclusion="Draft conclusion")
    finalized = create_report(
        stone=finalized_stone, user=admin_user, conclusion="Final conclusion"
    )
    finalized.is_finalized = True
    finalized.save(update_fields=["is_finalized"])

    response = auth_client(admin_user).get(
        "/api/v1/identification-reports/workflow-feed/"
    )

    assert response.status_code == 200
    assert response.data["count"] == 2
    rows = {(row["kind"], row["record_id"]): row for row in response.data["results"]}
    assert rows[("report", report.pk)]["status"] == "Draft"
    assert ("stone", bare_stone.pk) in rows
    bare_stone_row = rows[("stone", bare_stone.pk)]
    assert bare_stone_row["reference"] == bare_stone_row["detail"]["stone_reference"]
    assert ("report", finalized.pk) not in rows
    assert IdentificationReport.objects.filter(stone=stone).count() == 1
