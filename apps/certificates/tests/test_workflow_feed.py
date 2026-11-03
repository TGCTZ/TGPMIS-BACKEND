"""Certificates feed contains issued certificates, without a separate queue."""

from decimal import Decimal

import pytest

from apps.billing.dev import simulate_payment
from apps.billing.services import generate_bill_for_order
from apps.certificates.services import issue_certificate
from apps.gems.tests.factories import StoneTypeFactory
from apps.identification.tests.factories import (
    create_finalizable_report,
    finalize_complete_report,
)
from apps.orders.services import add_stone
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


def _ready_stone(settings, user):
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("50000.00")))
    simulate_payment(generate_bill_for_order(order))
    report = create_finalizable_report(stone, user)
    finalize_complete_report(report, user)
    return stone


def test_certificates_feed_contains_only_issued_certificates(
    settings, admin_user, auth_client
):
    """A finalized but unissued legacy record does not create an issue stage."""
    issued_stone = _ready_stone(settings, admin_user)
    certificate = issue_certificate(issued_stone, user=admin_user)
    waiting_stone = _ready_stone(settings, admin_user)

    response = auth_client(admin_user).get("/api/v1/certificates/workflow-feed/")

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["kind"] == "certificate"
    assert response.data["results"][0]["record_id"] == certificate.pk
    assert response.data["results"][0]["waiting"] is False
    assert all(row["record_id"] != waiting_stone.pk for row in response.data["results"])
