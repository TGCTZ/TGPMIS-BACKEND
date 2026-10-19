"""Bill generation, GePG payment notifications and settlement."""

from decimal import Decimal

import pytest

from django.core.management import call_command

from apps.billing.dev import simulate_payment
from apps.billing.models import Bill, BillItem, GePGCallback, Payment
from apps.billing.selectors import billing_worklist
from apps.billing.services import generate_bill_for_order
from apps.core.exceptions import ServiceError
from apps.gems.enums import BillStatus, StoneStatus
from apps.gems.tests.factories import StoneCategoryFactory, StoneTypeFactory
from apps.orders.models import StatusHistory
from apps.orders.services import add_stone
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def billable_order(settings):
    """An order whose three stones are all identified and priced."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=3)
    stone_type = StoneTypeFactory(price=Decimal("50000.00"))
    for _ in range(3):
        add_stone(order, stone_type=stone_type)
    return order


def test_generating_a_bill_prices_every_stone(billable_order):
    """The total is the sum of each stone's tier fee."""
    bill = generate_bill_for_order(billable_order)

    assert bill.bill_number.startswith("BILL-")
    bill_year, bill_sequence = bill.bill_number.split("-")[1:]
    order_year, order_sequence = billable_order.reference_number.split("-")[1:]
    assert bill_year == order_year
    assert int(bill_sequence) == int(order_sequence)
    assert bill.total_amount == Decimal("150000.00")
    assert bill.items.count() == 3
    assert bill.status == BillStatus.PENDING


def test_generating_a_bill_transitions_every_stone(billable_order):
    """Billing moves the whole order's stones together."""
    generate_bill_for_order(billable_order)

    statuses = set(billable_order.stones.values_list("status", flat=True))
    assert statuses == {StoneStatus.BILLED}


def test_bill_items_snapshot_the_price(billable_order):
    """Repricing a tier later must not rewrite an issued bill."""
    bill = generate_bill_for_order(billable_order)
    category = billable_order.stones.first().stone_type.category

    category.price = Decimal("999999.00")
    category.save(update_fields=["price"])

    bill.refresh_from_db()
    assert bill.total_amount == Decimal("150000.00")
    assert BillItem.objects.filter(bill=bill, amount=Decimal("50000.00")).count() == 3


def test_an_order_cannot_be_billed_twice(billable_order):
    """One bill per order; the second attempt is refused."""
    generate_bill_for_order(billable_order)

    with pytest.raises(ServiceError, match="already has a bill"):
        generate_bill_for_order(billable_order)


def test_an_unpriced_stone_type_refuses_to_bill(settings):
    """An unpriced type is a configuration gap, not a free identification."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory(price=None))

    with pytest.raises(ServiceError, match="No price set"):
        generate_bill_for_order(order)


def test_an_order_with_no_stones_refuses_to_bill(settings):
    """There is nothing to charge for."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=2)

    with pytest.raises(ServiceError, match="no stones"):
        generate_bill_for_order(order)


def test_payment_settles_the_bill_and_pays_the_stones(billable_order):
    """A full payment moves the bill and every stone to paid."""
    bill = generate_bill_for_order(billable_order)
    simulate_payment(bill)

    bill.refresh_from_db()
    assert bill.status == BillStatus.PAID
    assert set(billable_order.stones.values_list("status", flat=True)) == {
        StoneStatus.PAID
    }


def test_payment_is_attributed_to_the_system(billable_order):
    """The gateway has no user, so the history entry names no actor."""
    bill = generate_bill_for_order(billable_order)
    simulate_payment(bill)

    stone = billable_order.stones.first()
    entry = StatusHistory.objects.filter(stone=stone, to_status=StoneStatus.PAID).latest(
        "changed_at"
    )
    assert entry.changed_by is None
    assert entry.note == "Bill settled via GePG"


def test_a_redelivered_notification_is_idempotent(billable_order):
    """GePG redelivers until acknowledged; a repeat must not double-count.

    Without the ``trx_id`` guard the second delivery would record a second
    payment, re-transition every stone and write another history row each time.
    """
    from apps.billing.dev import _payment_xml
    from apps.billing.services import process_payment_notification

    bill = generate_bill_for_order(billable_order)
    bill.control_number = "991234567890"
    bill.save(update_fields=["control_number"])

    xml = _payment_xml(bill, bill.total_amount, "TRX-REPEAT-1")
    process_payment_notification(xml)
    process_payment_notification(xml)

    assert Payment.objects.filter(trx_id="TRX-REPEAT-1").count() == 1
    stone = billable_order.stones.first()
    assert (
        StatusHistory.objects.filter(stone=stone, to_status=StoneStatus.PAID).count() == 1
    )


def test_a_partial_payment_leaves_the_bill_unsettled(billable_order):
    """Stones stay billed until the full amount arrives."""
    from apps.billing.dev import _payment_xml
    from apps.billing.services import process_payment_notification

    bill = generate_bill_for_order(billable_order)
    bill.control_number = "991234567891"
    bill.save(update_fields=["control_number"])

    process_payment_notification(_payment_xml(bill, Decimal("50000.00"), "TRX-PARTIAL-1"))

    bill.refresh_from_db()
    assert bill.status == BillStatus.PARTIALLY_PAID
    assert set(billable_order.stones.values_list("status", flat=True)) == {
        StoneStatus.BILLED
    }


def test_an_unparseable_notification_returns_a_failure_ack():
    """A malformed body still gets well-formed XML, or GePG keeps retrying."""
    from apps.billing.services import process_payment_notification

    ack = process_payment_notification("not xml at all")

    assert "7242" in ack
    assert ack.strip().startswith("<")


def test_a_notification_matching_only_the_control_number_still_settles(billable_order):
    """The bill is looked up by number or by control number.

    GePG echoes back whichever identifier it holds, so a notification quoting an
    unrecognised bill id but the right control number must still be applied.
    """
    from apps.billing.dev import _payment_xml
    from apps.billing.services import process_payment_notification

    bill = generate_bill_for_order(billable_order)
    assert bill.control_number, "simulate mode should have issued a control number"

    xml = _payment_xml(bill, bill.total_amount, "TRX-CTRL-1").replace(
        bill.bill_number, "BILL-9999-9999"
    )
    ack = process_payment_notification(xml)

    assert "7101" in ack
    bill.refresh_from_db()
    assert bill.status == BillStatus.PAID


def test_a_notification_for_an_unknown_bill_returns_a_failure_ack(billable_order):
    """An unmatched bill is a failure ack, not an exception."""
    from apps.billing.dev import _payment_xml
    from apps.billing.services import process_payment_notification

    bill = generate_bill_for_order(billable_order)
    xml = (
        _payment_xml(bill, bill.total_amount, "TRX-GHOST-1")
        .replace(bill.bill_number, "BILL-9999-9999")
        .replace(bill.control_number, "990000000000")
    )

    ack = process_payment_notification(xml)

    assert "7204" in ack
    assert not Payment.objects.filter(trx_id="TRX-GHOST-1").exists()


def test_billing_worklist_holds_only_fully_identified_unbilled_orders(settings):
    """An order enters the queue when its last stone is typed, and leaves when billed."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=2)
    stone_type = StoneTypeFactory(price=Decimal("1000.00"))

    add_stone(order, stone_type=stone_type)
    assert order not in billing_worklist()

    add_stone(order, stone_type=stone_type)
    assert order in billing_worklist()

    generate_bill_for_order(order)
    assert order not in billing_worklist()


def test_generate_endpoint_requires_the_generate_permission(
    billable_order, viewer_user, auth_client
):
    """Billing is the accountant's job; a receptionist is refused."""
    response = auth_client(viewer_user).post(
        "/api/v1/bills/generate/", {"order": billable_order.pk}
    )

    assert response.status_code == 403


def test_generate_endpoint_creates_the_bill(billable_order, admin_user, auth_client):
    """The endpoint delegates to the service and returns the bill."""
    response = auth_client(admin_user).post(
        "/api/v1/bills/generate/", {"order": billable_order.pk}
    )

    assert response.status_code == 201, response.data
    assert response.data["total_amount"] == "150000.00"
    assert len(response.data["items"]) == 3


def test_bills_are_not_client_writable(billable_order, admin_user, auth_client):
    """A bill is written by the service and the gateway, never by a client."""
    bill = generate_bill_for_order(billable_order)

    response = auth_client(admin_user).patch(
        f"/api/v1/bills/{bill.pk}/", {"status": BillStatus.PAID}
    )

    assert response.status_code == 405
    bill.refresh_from_db()
    assert bill.status == BillStatus.PENDING


def test_payment_webhook_needs_no_authentication(billable_order, client, settings):
    """The gateway has no credentials; the callback must not 401.

    This is the URL GePG posts to, so a 401 here would silently strand every
    payment in the system.
    """
    from apps.billing.dev import _payment_xml

    # Callback signature behavior is covered independently; this test focuses
    # on the registered endpoint using the local development payload.
    settings.GEPG_USE_DIGITAL_SIGNATURE = False
    bill = generate_bill_for_order(billable_order)
    bill.control_number = "991234567892"
    bill.save(update_fields=["control_number"])
    xml = _payment_xml(bill, bill.total_amount, "TRX-WEBHOOK-1")

    response = client.post(
        "/gepg/payments/notification/", data=xml, content_type="application/xml"
    )
    duplicate_response = client.post(
        "/gepg/payments/notification/", data=xml, content_type="application/xml"
    )

    assert response.status_code == 200
    assert b"7101" in response.content
    assert duplicate_response.status_code == 200
    assert b"7101" in duplicate_response.content

    bill.refresh_from_db()
    assert bill.status == BillStatus.PENDING
    callback = GePGCallback.objects.get(kind=GePGCallback.Kind.PAYMENT)
    assert GePGCallback.objects.filter(kind=GePGCallback.Kind.PAYMENT).count() == 1
    assert callback.status == GePGCallback.Status.PENDING

    call_command("process_gepg_queue")

    bill.refresh_from_db()
    assert bill.status == BillStatus.PAID
    assert Payment.objects.filter(bill=bill).count() == 1


def test_listing_bills_does_not_n_plus_one(
    billable_order, admin_user, auth_client, django_assert_max_num_queries
):
    """Joins are explicit, so the query count does not grow with the page."""
    for _ in range(5):
        order = OrderFactory(stone_count=1)
        add_stone(order, stone_type=StoneTypeFactory(price=Decimal("1000.00")))
        generate_bill_for_order(order)

    client = auth_client(admin_user)
    with django_assert_max_num_queries(12):
        response = client.get("/api/v1/bills/")

    assert response.status_code == 200
    assert response.data["count"] == 5


def test_preview_prices_an_order_without_creating_anything():
    """The figures shown before anyone commits to them.

    Shares the pricing rule with generation rather than reimplementing it: the
    fee is per stone *category*, so a preview built from ``stone_type.price``
    would disagree with the bill it previews.
    """
    from apps.billing.services import preview_bill_for_order

    order = OrderFactory(stone_count=2)
    stone_type = StoneTypeFactory(category__price=Decimal("30000.00"))
    add_stone(order, stone_type=stone_type)
    add_stone(order, stone_type=stone_type)

    preview = preview_bill_for_order(order)

    assert len(preview["items"]) == 2
    assert preview["total"] == Decimal("60000.00")
    assert preview["blockers"] == []
    assert not Bill.objects.filter(order=order).exists(), "preview must not write"

    # And it agrees with what generation actually charges.
    bill = generate_bill_for_order(order)
    assert bill.total_amount == preview["total"]


def test_category_only_stones_can_be_priced_and_billed(settings):
    """Intake bills from the category before the bench records a type."""
    settings.GEPG_SIMULATE = True
    from apps.billing.services import preview_bill_for_order

    order = OrderFactory(stone_count=1)
    category = StoneCategoryFactory(name="Precious", price=Decimal("30000.00"))
    add_stone(order, stone_category=category)

    preview = preview_bill_for_order(order)
    assert preview["items"][0]["description"] == "Precious"
    assert preview["items"][0]["category"] == "Precious"
    assert preview["total"] == Decimal("30000.00")

    bill = generate_bill_for_order(order)
    item = BillItem.objects.get(bill=bill)
    assert item.description == "Precious"
    assert item.unit_price == Decimal("30000.00")


def test_preview_names_the_reason_an_order_cannot_be_billed():
    """An unpriced tier is reported per line, not raised.

    Generation refuses outright, but the screen needs to say *which* stone is
    the problem rather than just failing.
    """
    from apps.billing.services import preview_bill_for_order

    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory(category__price=None))

    preview = preview_bill_for_order(order)

    assert preview["items"][0]["amount"] is None
    assert any("No price set" in reason for reason in preview["blockers"])


def test_preview_endpoint_requires_the_generate_permission(viewer_user, auth_client):
    """Seeing what a customer will be charged is the billing clerk's job."""
    order = OrderFactory(stone_count=1)
    response = auth_client(viewer_user).get(f"/api/v1/bills/preview/?order={order.pk}")

    assert response.status_code == 403


def test_simulate_payment_endpoint_settles_a_bill(settings, admin_user, auth_client):
    """The dev-only button, going through the real notification handler."""
    settings.DEBUG = True
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory(category__price=Decimal("5000.00")))
    bill = generate_bill_for_order(order)

    response = auth_client(admin_user).post(f"/api/v1/bills/{bill.pk}/simulate-payment/")

    assert response.status_code == 200, response.data
    assert response.data["status"] == "paid"
    assert Decimal(response.data["amount_paid"]) == Decimal("5000.00")


def test_simulate_payment_endpoint_accepts_a_part_payment(
    settings, admin_user, auth_client
):
    """The only route to PARTIALLY_PAID, which nothing else can produce offline."""
    settings.DEBUG = True
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory(category__price=Decimal("5000.00")))
    bill = generate_bill_for_order(order)

    response = auth_client(admin_user).post(
        f"/api/v1/bills/{bill.pk}/simulate-payment/", {"amount": "2000.00"}
    )

    assert response.status_code == 200, response.data
    assert response.data["status"] == "partially_paid"

    # A second call tops it up rather than being swallowed as a redelivery.
    again = auth_client(admin_user).post(
        f"/api/v1/bills/{bill.pk}/simulate-payment/", {"amount": "3000.00"}
    )
    assert again.data["status"] == "paid"


def test_simulate_payment_is_invisible_outside_simulation(
    settings, admin_user, auth_client
):
    """404, not 403: the route must not advertise itself where it must not exist.

    Both flags are required. DEBUG alone would let a staging box pointed at the
    real gateway forge settlements.
    """
    settings.DEBUG = True
    settings.GEPG_SIMULATE = False
    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory(category__price=Decimal("5000.00")))
    bill = generate_bill_for_order(order)

    response = auth_client(admin_user).post(f"/api/v1/bills/{bill.pk}/simulate-payment/")

    assert response.status_code == 404
    bill.refresh_from_db()
    assert bill.status == "pending"


def test_config_reports_whether_simulation_is_available(
    settings, admin_user, auth_client
):
    """What the UI reads to decide whether to offer the button at all."""
    settings.DEBUG = True
    settings.GEPG_SIMULATE = True
    assert auth_client(admin_user).get("/api/v1/config/").data["simulate_payments"]

    settings.GEPG_SIMULATE = False
    assert not auth_client(admin_user).get("/api/v1/config/").data["simulate_payments"]


def test_billing_worklist_is_searchable(billable_order, admin_user, auth_client):
    """Searched by the customer as readily as by the reference.

    The action returns Orders while the ViewSet is a Bill one, so this covers the
    explicit whitelist it passes rather than the ViewSet's own `search_fields` -
    which would raise `FieldError` against an Order queryset.
    """
    client = auth_client(admin_user)
    customer = billable_order.customer

    for term in (billable_order.reference_number, customer.last_name, customer.phone):
        hit = client.get("/api/v1/bills/worklist/", {"search": term})
        assert hit.status_code == 200, hit.data
        assert [row["id"] for row in hit.data["results"]] == [billable_order.pk], term

    miss = client.get("/api/v1/bills/worklist/", {"search": "no-such-order"})
    assert miss.data["results"] == []
