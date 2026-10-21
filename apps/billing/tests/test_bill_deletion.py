"""Admin deletion rules for bills and their GePG control numbers."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from django.contrib.auth.models import Permission

from apps.billing.models import Bill, BillItem, Payment
from apps.gems.tests.factories import StoneTypeFactory
from apps.orders.services import add_stone
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


def _bill(order):
    """Create a pending local bill without contacting the gateway."""
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("50000.00")))
    bill = Bill.objects.create(
        order=order,
        bill_number=f"TEST-BILL-{order.pk}",
        total_amount=Decimal("50000.00"),
    )
    BillItem.objects.create(
        bill=bill,
        stone=stone,
        description="Stone fee",
        unit_price=Decimal("50000.00"),
        amount=Decimal("50000.00"),
    )
    return bill


def _allow_deletion(user):
    permission = Permission.objects.get(
        content_type__app_label="billing", codename="delete_bill"
    )
    user.user_permissions.add(permission)


def test_admin_soft_deletes_unsubmitted_bill_and_items(api_client, user):
    """Local bills can be deleted and remain available to audit history."""
    bill = _bill(OrderFactory(stone_count=1))
    item_id = bill.items.get().pk
    _allow_deletion(user)
    api_client.force_authenticate(user)

    response = api_client.delete(f"/api/v1/bills/{bill.pk}/")

    assert response.status_code == 204, response.data
    assert Bill.objects.filter(pk=bill.pk).count() == 0
    assert Bill.all_objects.get(pk=bill.pk).is_deleted
    assert BillItem.all_objects.get(pk=item_id).is_deleted


def test_admin_delete_of_bill_with_control_number_requires_gepg_confirmation(
    api_client, user
):
    """A control number is cancelled before local history is hidden."""
    bill = _bill(OrderFactory(stone_count=1))
    bill.control_number = "991234567890"
    bill.save(update_fields=["control_number"])
    _allow_deletion(user)
    api_client.force_authenticate(user)

    with patch(
        "apps.billing.views.cancel_bill",
        return_value=SimpleNamespace(status_code="7101", status_desc="Pending"),
    ) as cancel:
        response = api_client.delete(f"/api/v1/bills/{bill.pk}/")

    assert response.status_code == 409
    assert Bill.objects.filter(pk=bill.pk).exists()
    cancel.assert_called_once()


def test_admin_delete_soft_deletes_bill_after_gepg_confirmation(api_client, user):
    """The local bill is hidden only after GePG returns its confirmed code."""
    bill = _bill(OrderFactory(stone_count=1))
    bill.is_gepg_submitted = True
    bill.save(update_fields=["is_gepg_submitted"])
    _allow_deletion(user)
    api_client.force_authenticate(user)

    with patch(
        "apps.billing.views.cancel_bill",
        return_value=SimpleNamespace(status_code="7283", status_desc="Cancelled"),
    ):
        response = api_client.delete(f"/api/v1/bills/{bill.pk}/")

    assert response.status_code == 204
    assert Bill.all_objects.get(pk=bill.pk).is_deleted


def test_any_payment_history_blocks_admin_deletion(api_client, user):
    """Any recorded payment protects the bill, even if its amount is small."""
    bill = _bill(OrderFactory(stone_count=1))
    Payment.objects.create(
        bill=bill,
        trx_id="TRX-PARTIAL",
        paid_amount=Decimal("1.00"),
        bill_amount=bill.total_amount,
    )
    _allow_deletion(user)
    api_client.force_authenticate(user)

    response = api_client.delete(f"/api/v1/bills/{bill.pk}/")

    assert response.status_code == 409
    assert Bill.objects.filter(pk=bill.pk).exists()
