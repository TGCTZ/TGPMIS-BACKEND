"""The timed GePG order-copy workflow."""

from datetime import timedelta
from decimal import Decimal

import pytest

from django.utils import timezone

from apps.billing.models import Bill
from apps.core.services import reference_number_for_order
from apps.gems.enums import BillStatus, StoneStatus, WeightUnit
from apps.gems.tests.factories import InstrumentFactory, StoneTypeFactory
from apps.identification.models import IdentificationReport, InstrumentUsed
from apps.orders.models import Order, StatusHistory, Stone
from apps.orders.tests.factories import OrderFactory, StoneFactory

pytestmark = pytest.mark.django_db


def _pending_bill(order, *, submitted_at=None, control_number=""):
    return Bill.objects.create(
        order=order,
        bill_number=f"BILL-{order.pk}",
        status=BillStatus.PENDING,
        control_number=control_number,
        is_gepg_submitted=True,
        gepg_submitted_at=submitted_at or timezone.now() - timedelta(minutes=11),
    )


def test_order_exposes_configured_copy_available_time(settings, admin_user, auth_client):
    """The API reports the server-configured eligibility timestamp."""
    settings.GEPG_PENDING_ORDER_COPY_AFTER_SECONDS = 90
    order = OrderFactory()
    submitted_at = timezone.now() - timedelta(minutes=2)
    _pending_bill(order, submitted_at=submitted_at)

    response = auth_client(admin_user).get(f"/api/v1/orders/{order.pk}/")

    assert response.status_code == 200
    assert response.data["copy_available_at"] == submitted_at + timedelta(seconds=90)


def test_order_copy_action_waits_until_the_configured_timeout(
    settings, admin_user, auth_client
):
    """A copy request before the timeout must not create an order."""
    settings.GEPG_PENDING_ORDER_COPY_AFTER_SECONDS = 600
    order = OrderFactory(stone_count=1)
    StoneFactory(order=order)
    _pending_bill(order, submitted_at=timezone.now() - timedelta(seconds=599))

    response = auth_client(admin_user).post(
        f"/api/v1/orders/{order.pk}/copy-after-gepg-timeout/"
    )

    assert response.status_code == 400
    assert "timeout has elapsed" in str(response.data)
    assert Order.objects.count() == 1


def test_order_copy_duplicates_working_data_but_not_billing_or_history(
    settings, admin_user, auth_client
):
    """The copy gets fresh identities and no duplicated financial history."""
    settings.GEPG_PENDING_ORDER_COPY_AFTER_SECONDS = 600
    source = OrderFactory(stone_count=1)
    stone_type = StoneTypeFactory(price=Decimal("30000.00"))
    source_stone = StoneFactory(
        order=source,
        label="A",
        stone_type=stone_type,
        stone_category=stone_type.category,
        weight=Decimal("1.250"),
        weight_unit=WeightUnit.CARAT,
        status=StoneStatus.BILLED,
        photo="stones/source.jpg",
    )
    StatusHistory.objects.create(
        stone=source_stone,
        from_status="",
        to_status=StoneStatus.RECEIVED,
        note="Original intake",
    )
    StatusHistory.objects.create(
        stone=source_stone,
        from_status=StoneStatus.RECEIVED,
        to_status=StoneStatus.BILLED,
        note="Original bill",
    )
    source_report = IdentificationReport.objects.create(
        stone=source_stone,
        report_number=reference_number_for_order(source, "TGC", stone_label="A"),
        conclusion="Natural garnet",
        is_finalized=True,
        identified_by=admin_user,
        identified_at=timezone.now(),
    )
    instrument = InstrumentFactory()
    InstrumentUsed.objects.create(report=source_report, instrument=instrument)
    source_bill = _pending_bill(source)

    response = auth_client(admin_user).post(
        f"/api/v1/orders/{source.pk}/copy-after-gepg-timeout/"
    )

    assert response.status_code == 201, response.data
    copied = Order.objects.get(pk=response.data["id"])
    copied_stone = Stone.objects.get(order=copied)
    copied_report = IdentificationReport.objects.get(stone=copied_stone)

    assert copied.pk != source.pk
    assert copied.reference_number != source.reference_number
    assert copied.customer_id == source.customer_id
    assert copied.received_date == source.received_date
    assert copied.stone_count == source.stone_count
    assert not hasattr(copied, "bill")
    assert copied_stone.label == source_stone.label
    assert copied_stone.stone_category_id == source_stone.stone_category_id
    assert copied_stone.stone_type_id == source_stone.stone_type_id
    assert copied_stone.weight == source_stone.weight
    assert copied_stone.weight_unit == source_stone.weight_unit
    assert copied_stone.status == source_stone.status
    assert copied_stone.photo.name == source_stone.photo.name
    assert copied_report.report_number == reference_number_for_order(
        copied, "TGC", stone_label=copied_stone.label
    )
    assert copied_report.report_number != source_report.report_number
    assert copied_report.conclusion == source_report.conclusion
    assert copied_report.is_finalized is True
    assert copied_report.identified_by_id == source_report.identified_by_id
    assert list(
        copied_report.instruments_used.values_list("instrument_id", flat=True)
    ) == [instrument.pk]
    assert StatusHistory.objects.filter(stone=source_stone).count() == 2
    copied_history = StatusHistory.objects.get(stone=copied_stone)
    assert copied_history.from_status == ""
    assert copied_history.to_status == source_stone.status
    assert copied_history.note == f"Copied from {source.reference_number}"
    assert Bill.objects.count() == 1
    assert Bill.objects.get(pk=source_bill.pk).order_id == source.pk


def test_order_copy_is_refused_if_gepg_returns_control_number(
    settings, admin_user, auth_client
):
    """A late control number makes the copy endpoint refuse the request."""
    settings.GEPG_PENDING_ORDER_COPY_AFTER_SECONDS = 60
    order = OrderFactory(stone_count=1)
    StoneFactory(order=order)
    _pending_bill(
        order,
        submitted_at=timezone.now() - timedelta(minutes=2),
        control_number="991234567890",
    )

    response = auth_client(admin_user).post(
        f"/api/v1/orders/{order.pk}/copy-after-gepg-timeout/"
    )

    assert response.status_code == 400
    assert "no longer has an eligible" in str(response.data)
    assert Order.objects.count() == 1


def test_order_copy_requires_order_creation_permission(user, auth_client):
    """Only users allowed to create orders may copy a pending order."""
    order = OrderFactory()
    _pending_bill(order)

    response = auth_client(user).post(
        f"/api/v1/orders/{order.pk}/copy-after-gepg-timeout/"
    )

    assert response.status_code == 403
