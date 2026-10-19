"""Order intake, identification and status transitions."""

from decimal import Decimal

import pytest

from apps.billing.services import generate_bill_for_order
from apps.core.exceptions import ServiceError
from apps.core.services import format_reference_number
from apps.gems.enums import StoneStatus, WeightUnit
from apps.gems.tests.factories import StoneCategoryFactory, StoneTypeFactory
from apps.orders.models import Customer, Order, StatusHistory, Stone
from apps.orders.selectors import identification_worklist
from apps.orders.services import add_stone, create_order, transition_stone, update_stone
from apps.orders.tests.factories import CustomerFactory, OrderFactory, StoneFactory

pytestmark = pytest.mark.django_db


def test_create_order_allocates_a_reference_number():
    """The service, not the client, mints ORD-<fy>-NNNNN."""
    order = create_order(customer=CustomerFactory(), stone_count=2)

    assert order.reference_number.startswith("ORD-")
    assert order.reference_number.endswith("-00001")


def test_reference_numbers_do_not_reuse_a_deleted_one():
    """Numbering scans all rows, so a soft-deleted number is never reissued.

    Reusing a reference number would make two different orders indistinguishable
    in any record that kept only the string.
    """
    first = create_order(customer=CustomerFactory(), stone_count=1)
    first.delete()
    second = create_order(customer=CustomerFactory(), stone_count=1)

    assert second.reference_number != first.reference_number


def test_add_stone_labels_stones_in_sequence():
    """Stones are lettered in the order they are identified."""
    order = OrderFactory(stone_count=3)
    stone_type = StoneTypeFactory()

    labels = [
        add_stone(order, stone_type=stone_type).label,
        add_stone(order, stone_type=stone_type).label,
        add_stone(order, stone_type=stone_type).label,
    ]

    assert labels == ["A", "B", "C"]


def test_add_stone_refuses_more_than_the_customer_submitted():
    """The count the customer declared is the cap.

    Registering a fourth stone against a three-stone order would mean the lab is
    holding something nobody handed over.
    """
    order = OrderFactory(stone_count=1)
    stone_type = StoneTypeFactory()
    add_stone(order, stone_type=stone_type)

    with pytest.raises(ServiceError):
        add_stone(order, stone_type=stone_type)


def test_add_stone_records_the_first_history_entry():
    """A newly identified stone starts its trail with an entry from nowhere."""
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory())

    entry = StatusHistory.objects.get(stone=stone)
    assert entry.from_status == ""
    assert entry.to_status == StoneStatus.RECEIVED


def test_transition_writes_history_and_records_the_actor(user):
    """Every status change leaves a trail naming who made it."""
    stone = StoneFactory()
    transition_stone(stone, StoneStatus.BILLED, user=user, note="Billed on BILL-1")

    stone.refresh_from_db()
    assert stone.status == StoneStatus.BILLED

    entry = StatusHistory.objects.filter(stone=stone).latest("changed_at")
    assert entry.from_status == StoneStatus.RECEIVED
    assert entry.to_status == StoneStatus.BILLED
    assert entry.changed_by == user
    assert entry.note == "Billed on BILL-1"


def test_transition_to_the_same_status_is_a_no_op():
    """A redelivered notification must not double up the history."""
    stone = StoneFactory(status=StoneStatus.PAID)
    transition_stone(stone, StoneStatus.PAID)

    assert not StatusHistory.objects.filter(stone=stone).exists()


def test_transition_without_a_user_is_attributed_to_the_system():
    """A payment callback has no request user, and that must not fail."""
    stone = StoneFactory()
    transition_stone(stone, StoneStatus.PAID, note="Bill settled via GePG")

    entry = StatusHistory.objects.filter(stone=stone).latest("changed_at")
    assert entry.changed_by is None


def test_preliminary_worklist_holds_only_incomplete_orders():
    """The queue empties as the last stone is identified."""
    order = OrderFactory(stone_count=2)
    stone_type = StoneTypeFactory()

    add_stone(order, stone_type=stone_type)
    assert order in identification_worklist()

    add_stone(order, stone_type=stone_type)
    assert order not in identification_worklist()


def test_order_create_endpoint_delegates_to_the_service(admin_user, auth_client):
    """POST /orders/ mints the reference number rather than trusting the client."""
    customer = CustomerFactory()
    response = auth_client(admin_user).post(
        "/api/v1/orders/",
        {
            "customer": customer.pk,
            "stone_count": 2,
            "received_date": "2026-09-09",
            "reference_number": "CLIENT-SUPPLIED",
        },
    )

    assert response.status_code == 201, response.data
    assert response.data["reference_number"].startswith("ORD-")
    assert Order.objects.get(pk=response.data["id"]).stone_count == 2


def test_add_stone_endpoint_identifies_and_caps(admin_user, auth_client):
    """The cap surfaces as a 400 carrying the service's message."""
    order = OrderFactory(stone_count=1)
    stone_type = StoneTypeFactory()
    client = auth_client(admin_user)

    first = client.post(
        f"/api/v1/orders/{order.pk}/stones/", {"stone_type": stone_type.pk}
    )
    assert first.status_code == 201, first.data
    assert first.data["label"] == "A"

    second = client.post(
        f"/api/v1/orders/{order.pk}/stones/", {"stone_type": stone_type.pk}
    )
    assert second.status_code == 400
    assert "already been identified" in str(second.data)


def test_identify_stones_endpoint_accepts_category_without_a_type(
    admin_user, auth_client
):
    """Intake can choose a pricing tier before the bench records exact type."""
    order = OrderFactory(stone_count=1)
    category = StoneCategoryFactory(name="Precious", price=Decimal("30000.00"))

    response = auth_client(admin_user).post(
        f"/api/v1/orders/{order.pk}/stones/", {"stone_category": category.pk}
    )

    assert response.status_code == 201, response.data
    assert response.data["stone_category"] == category.pk
    assert response.data["stone_category_detail"]["name"] == "Precious"
    assert response.data["stone_type"] is None


def test_identify_stones_rejects_a_type_from_another_category(admin_user, auth_client):
    """Legacy payloads still cannot pair a category with an unrelated type."""
    order = OrderFactory(stone_count=1)
    category = StoneCategoryFactory(name="Precious")
    stone_type = StoneTypeFactory()

    response = auth_client(admin_user).post(
        f"/api/v1/orders/{order.pk}/stones/",
        {"stone_category": category.pk, "stone_type": stone_type.pk},
    )

    assert response.status_code == 400
    assert "belonging to this category" in str(response.data)


def test_transition_endpoint_requires_the_transition_permission(admin_user, auth_client):
    """A custom action is gated on its own permission, not on add_stone.

    Regression test for the whole point of ActionPermissions: the transition
    route is a POST, so the method-based map would ask for ``add_stone`` and let
    anyone who can identify a stone also move it to certified.
    """
    from django.contrib.auth.models import Permission

    stone = StoneFactory()
    admin_user.user_permissions.add(
        *Permission.objects.filter(
            codename__in=["view_stone", "add_stone", "change_stone"]
        )
    )
    admin_user.groups.clear()

    response = auth_client(admin_user).post(
        f"/api/v1/stones/{stone.pk}/transition/", {"to_status": StoneStatus.CERTIFIED}
    )

    assert response.status_code == 403


def test_transition_endpoint_writes_history(admin_user, auth_client):
    """The endpoint records the acting user in the trail."""
    stone = StoneFactory()
    response = auth_client(admin_user).post(
        f"/api/v1/stones/{stone.pk}/transition/",
        {"to_status": StoneStatus.BILLED, "note": "via API"},
    )

    assert response.status_code == 200, response.data
    assert response.data["status"] == StoneStatus.BILLED

    entry = StatusHistory.objects.filter(stone=stone).latest("changed_at")
    assert entry.changed_by == admin_user
    assert entry.note == "via API"


def test_stone_status_is_not_directly_writable(admin_user, auth_client):
    """PATCHing status must not bypass the history.

    If the field were writable, a client could move a stone to certified without
    leaving any record of who did it or when.
    """
    stone = StoneFactory()
    response = auth_client(admin_user).patch(
        f"/api/v1/stones/{stone.pk}/", {"status": StoneStatus.CERTIFIED}
    )

    assert response.status_code == 200, response.data
    stone.refresh_from_db()
    assert stone.status == StoneStatus.RECEIVED


def test_listing_stones_does_not_n_plus_one(
    admin_user, auth_client, django_assert_max_num_queries
):
    """select_related keeps the query count flat as rows grow."""
    order = OrderFactory(stone_count=10)
    stone_type = StoneTypeFactory()
    for _ in range(10):
        add_stone(order, stone_type=stone_type)

    client = auth_client(admin_user)
    with django_assert_max_num_queries(10):
        response = client.get("/api/v1/stones/")

    assert response.status_code == 200
    assert response.data["count"] == 10


def test_status_history_is_read_only(admin_user, auth_client):
    """The ledger cannot be written through the API.

    Refused twice over: the viewset is read-only so no create route exists, and
    no role is granted ``add_statushistory``. Which of the two answers first is
    an implementation detail - the guarantee is that nothing is written.
    """
    stone = StoneFactory()
    transition_stone(stone, StoneStatus.BILLED)

    client = auth_client(admin_user)
    listing = client.get(f"/api/v1/status-history/?filter[stone]={stone.pk}")
    assert listing.status_code == 200
    assert listing.data["count"] == 1

    before = StatusHistory.objects.count()
    write = client.post(
        "/api/v1/status-history/",
        {"stone": stone.pk, "to_status": StoneStatus.PAID},
    )

    assert write.status_code in (403, 405)
    assert StatusHistory.objects.count() == before


def test_an_order_can_register_its_customer(admin_user, auth_client):
    """Reception meets most customers while receiving their first order."""
    response = auth_client(admin_user).post(
        "/api/v1/orders/",
        {
            "customer_data": {
                "first_name": "Asha",
                "last_name": "Mwinyi",
                "phone": "0754123456",
            },
            "stone_count": 2,
            "received_date": "2026-09-13",
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["customer_detail"]["full_name"] == "Asha Mwinyi"
    assert Customer.objects.filter(phone="0754123456").count() == 1


def test_an_order_refuses_both_a_customer_and_new_details(admin_user, auth_client):
    """Sending both means the client does not know whose order this is."""
    before = Order.objects.count()

    response = auth_client(admin_user).post(
        "/api/v1/orders/",
        {
            "customer": CustomerFactory().pk,
            "customer_data": {
                "first_name": "Asha",
                "last_name": "Mwinyi",
                "phone": "0754123456",
            },
            "stone_count": 1,
            "received_date": "2026-09-13",
        },
        format="json",
    )

    assert response.status_code == 400
    assert Order.objects.count() == before


def test_an_order_needs_a_customer_one_way_or_the_other(admin_user, auth_client):
    """Neither field given is the ordinary missing-required-field case."""
    response = auth_client(admin_user).post(
        "/api/v1/orders/",
        {"stone_count": 1, "received_date": "2026-09-13"},
        format="json",
    )

    assert response.status_code == 400
    assert "customer" in response.data


def test_registering_a_customer_needs_the_customer_permission(user, auth_client, roles):
    """The nested write must not route around orders.add_customer."""
    from django.contrib.auth.models import Permission

    user.user_permissions.add(
        Permission.objects.get(codename="add_order", content_type__app_label="orders"),
        Permission.objects.get(codename="view_order", content_type__app_label="orders"),
    )

    response = auth_client(user).post(
        "/api/v1/orders/",
        {
            "customer_data": {
                "first_name": "Asha",
                "last_name": "Mwinyi",
                "phone": "0754123456",
            },
            "stone_count": 1,
            "received_date": "2026-09-13",
        },
        format="json",
    )

    assert response.status_code == 403
    assert not Customer.objects.filter(phone="0754123456").exists()


def test_a_failed_order_leaves_no_orphan_customer(monkeypatch):
    """The customer is created inside the order's transaction, not before it."""
    import apps.orders.services.order as order_service

    def boom(*args, **kwargs):
        raise RuntimeError("reference allocation failed")

    monkeypatch.setattr(order_service, "generate_reference_number", boom)

    with pytest.raises(RuntimeError):
        create_order(
            customer_data={
                "first_name": "Asha",
                "last_name": "Mwinyi",
                "phone": "0754123456",
            },
            stone_count=1,
        )

    assert not Customer.objects.filter(phone="0754123456").exists()


def test_an_order_can_register_a_customer_with_an_existing_phone(admin_user, auth_client):
    """Phone numbers may be shared by customers, so they are not identifiers."""
    existing = CustomerFactory(first_name="Asha", last_name="Mwinyi", phone="0754123456")

    response = auth_client(admin_user).post(
        "/api/v1/orders/",
        {
            "customer_data": {
                "first_name": "Aisha",
                "last_name": "M",
                "phone": "0754123456",
            },
            "stone_count": 1,
            "received_date": "2026-09-13",
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["customer"] != existing.pk
    assert Customer.objects.filter(phone="0754123456").count() == 2


def test_a_gemmologist_may_identify_a_stone(gemmologist_user, auth_client):
    """Identification is the bench's work, so the bench can do it."""
    order = OrderFactory(stone_count=1)

    response = auth_client(gemmologist_user).post(
        f"/api/v1/orders/{order.pk}/stones/", {"stone_type": StoneTypeFactory().pk}
    )

    assert response.status_code == 201, response.data
    assert response.data["label"] == "A"


def test_a_receptionist_may_no_longer_identify_a_stone(viewer_user, auth_client):
    """Reception records how many stones arrived, not what they are.

    Typing a stone fixes its price, so it belongs to the gemmologist. Reception
    keeps `view_stone` and `transition_stone` for handover.
    """
    order = OrderFactory(stone_count=1)

    response = auth_client(viewer_user).post(
        f"/api/v1/orders/{order.pk}/stones/", {"stone_type": StoneTypeFactory().pk}
    )

    assert response.status_code == 403
    assert not order.stones.exists()


def test_the_preliminary_worklist_belongs_to_the_bench(
    gemmologist_user, viewer_user, auth_client
):
    """The queue is gated on the verb that works it, not on viewing orders."""
    assert (
        auth_client(gemmologist_user).get("/api/v1/orders/worklist/").status_code == 200
    )
    assert auth_client(viewer_user).get("/api/v1/orders/worklist/").status_code == 403


def test_a_receptionist_may_still_hand_a_stone_over(viewer_user, auth_client):
    """Narrowing reception's stone permissions must not break handover."""
    stone = StoneFactory()

    response = auth_client(viewer_user).post(
        f"/api/v1/stones/{stone.pk}/transition/",
        {"to_status": StoneStatus.COLLECTED, "note": "Collected at the front desk"},
    )

    assert response.status_code == 200, response.data


def test_identification_does_not_record_a_weight(admin_user, auth_client):
    """Identification assigns a type; the bench weighs the stone later.

    A weight in the payload is ignored rather than refused - the field does not
    exist here any more, and 400-ing a client that has not caught up would block
    an identification over a value we discard anyway.
    """
    order = OrderFactory(stone_count=1)

    response = auth_client(admin_user).post(
        f"/api/v1/orders/{order.pk}/stones/",
        {"stone_type": StoneTypeFactory().pk, "weight": "2.500"},
    )

    assert response.status_code == 201, response.data
    assert response.data["weight"] is None

    stone = Stone.objects.get(pk=response.data["id"])
    assert stone.weight is None
    assert stone.weight_unit == WeightUnit.CARAT


def test_identification_filter_splits_the_list_in_two(admin_user, auth_client):
    """Pending and complete are disjoint, and together are the whole list."""
    stone_type = StoneTypeFactory()
    pending = OrderFactory(stone_count=2)
    add_stone(pending, stone_type=stone_type)
    complete = OrderFactory(stone_count=1)
    add_stone(complete, stone_type=stone_type)

    client = auth_client(admin_user)

    def refs(params=""):
        response = client.get(f"/api/v1/orders/{params}")
        assert response.status_code == 200, response.data
        return {row["reference_number"] for row in response.data["results"]}

    assert refs("?identification=pending") == {
        format_reference_number(pending.reference_number)
    }
    assert refs("?identification=complete") == {
        format_reference_number(complete.reference_number)
    }
    assert refs() == {
        format_reference_number(pending.reference_number),
        format_reference_number(complete.reference_number),
    }


def test_stage_and_identification_filters_combine(admin_user, auth_client, user):
    """A hold outranks identification, so the stage alone is not enough.

    Both orders below are on hold; only one has every stone typed. The
    Identification page asks for finished identification *and* a stage, and a
    stage that silently replaced the identification filter would slip the
    half-typed order back onto a screen meant only for finished ones.
    """
    from apps.gems.enums import OrderHold
    from apps.orders.services import hold_order

    stone_type = StoneTypeFactory()
    partial = OrderFactory(stone_count=2)
    add_stone(partial, stone_type=stone_type)
    full = OrderFactory(stone_count=1)
    add_stone(full, stone_type=stone_type)
    for order in (partial, full):
        hold_order(order, status=OrderHold.ON_HOLD, reason="Customer away.", user=user)

    client = auth_client(admin_user)

    def refs(params):
        response = client.get(f"/api/v1/orders/{params}")
        assert response.status_code == 200, response.data
        return {row["reference_number"] for row in response.data["results"]}

    # The stage by itself still returns both - that is what a hold means.
    assert refs("?stage=on_hold") == {
        format_reference_number(partial.reference_number),
        format_reference_number(full.reference_number),
    }
    assert refs("?stage=on_hold&identification=complete") == {
        format_reference_number(full.reference_number)
    }
    assert refs("?stage=on_hold&identification=pending") == {
        format_reference_number(partial.reference_number)
    }


def test_an_unknown_identification_value_is_ignored(admin_user, auth_client):
    """A stale link degrades to the plain list rather than erroring."""
    OrderFactory(stone_count=1)

    response = auth_client(admin_user).get("/api/v1/orders/?identification=nonsense")

    assert response.status_code == 200, response.data
    assert response.data["count"] == 1


def test_a_billed_stone_cannot_change_category(settings, admin_user, auth_client):
    """The saved category priced the bill, so changing it would falsify it."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("30000.00")))
    generate_bill_for_order(order)
    stone.refresh_from_db()

    response = auth_client(admin_user).patch(
        f"/api/v1/stones/{stone.pk}/",
        {"stone_category": StoneCategoryFactory().pk},
    )

    assert response.status_code == 400
    assert "priced the bill" in str(response.data)

    original = stone.stone_category_id
    stone.refresh_from_db()
    assert stone.stone_category_id == original


def test_a_billed_stone_still_accepts_its_weight(settings, admin_user, auth_client):
    """Weight arrives after billing by design - the bench weighs at the findings."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("30000.00")))
    generate_bill_for_order(order)
    stone.refresh_from_db()

    response = auth_client(admin_user).patch(
        f"/api/v1/stones/{stone.pk}/", {"weight": "4.250"}
    )

    assert response.status_code == 200, response.data
    stone.refresh_from_db()
    assert stone.weight == Decimal("4.250")


def test_a_received_stone_can_still_be_retyped(admin_user, auth_client):
    """The lock starts at billing, not before."""
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory())
    wanted = StoneTypeFactory()

    response = auth_client(admin_user).patch(
        f"/api/v1/stones/{stone.pk}/", {"stone_type": wanted.pk}
    )

    assert response.status_code == 200, response.data
    stone.refresh_from_db()
    assert stone.stone_type_id == wanted.pk


def test_a_billed_stone_cannot_be_deleted(settings, admin_user, auth_client):
    """Deletion bypasses the service, so the viewset repeats the guard."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("30000.00")))
    generate_bill_for_order(order)

    response = auth_client(admin_user).delete(f"/api/v1/stones/{stone.pk}/")

    assert response.status_code == 400
    stone.refresh_from_db()
    assert stone.deleted_at is None


def test_stone_photo_can_be_recorded_and_cleared(settings):
    """The bench photograph, which the certificate prints.

    Uses the ``_UNSET`` sentinel rather than a ``None`` default, so an update
    that does not mention the photo leaves it alone - the bug this guards
    against is a partial update silently wiping the image.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory())

    # A 1x1 GIF: the smallest thing Pillow will accept as an image.
    image = SimpleUploadedFile(
        "stone.gif",
        b"GIF89a\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00"
        b"\x01\x00\x01\x00\x00\x02\x00;",
        content_type="image/gif",
    )
    update_stone(stone, photo=image)
    stone.refresh_from_db()
    assert stone.photo

    # An unrelated update must not disturb it.
    update_stone(stone, weight=Decimal("1.500"))
    stone.refresh_from_db()
    assert stone.photo, "a partial update must not clear the photograph"

    # Passing None explicitly does clear it.
    update_stone(stone, photo=None)
    stone.refresh_from_db()
    assert not stone.photo


def test_order_reports_its_bill_number(settings, admin_user, auth_client):
    """So a screen can tell "ready to bill" from "already billed".

    Without this the Orders table cannot distinguish the two, and offers to bill
    an order that is already awaiting payment.
    """
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory(category__price=Decimal("1000.00")))
    client = auth_client(admin_user)

    before = client.get(f"/api/v1/orders/{order.pk}/")
    assert before.data["bill_number"] is None
    assert before.data["bill_total_amount"] is None
    assert before.data["bill_currency"] is None

    bill = generate_bill_for_order(order)

    after = client.get(f"/api/v1/orders/{order.pk}/")
    assert after.data["bill_number"] == format_reference_number(bill.bill_number)
    assert after.data["bill_total_amount"] == "1000.00"
    assert after.data["bill_currency"] == "TZS"


def test_order_stage_follows_the_least_advanced_stone(settings, user):
    """An order is only as far along as the stone furthest behind.

    A customer collecting their stones cares about the last one, not the first,
    so the summary must not average away a stone still on the bench.
    """
    from apps.billing.dev import simulate_payment
    from apps.gems.enums import OrderStage
    from apps.orders.selectors import order_stage

    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=2)
    stone_type = StoneTypeFactory(category__price=Decimal("1000.00"))

    assert order_stage(order) == OrderStage.EMPTY

    first = add_stone(order, stone_type=stone_type)
    assert order_stage(order) == OrderStage.IDENTIFYING, "one stone short"

    second = add_stone(order, stone_type=stone_type)
    order.refresh_from_db()
    assert order_stage(order) == OrderStage.READY_TO_BILL

    bill = generate_bill_for_order(order)
    order.refresh_from_db()
    assert order_stage(order) == OrderStage.AWAITING_PAYMENT

    simulate_payment(bill)
    order.refresh_from_db()
    assert order_stage(order) == OrderStage.IN_FINDINGS

    # One stone certified is not a certified order.
    transition_stone(first, StoneStatus.CERTIFIED, user=user)
    order.refresh_from_db()
    assert order_stage(order) == OrderStage.IN_FINDINGS, "the other is still behind"

    transition_stone(second, StoneStatus.CERTIFIED, user=user)
    order.refresh_from_db()
    assert order_stage(order) == OrderStage.CERTIFIED


def test_a_held_stone_dominates_the_order_stage(user):
    """An exception anywhere is the row a supervisor needs to find."""
    from apps.gems.enums import OrderStage
    from apps.orders.selectors import order_stage

    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory())

    transition_stone(stone, StoneStatus.ON_HOLD, user=user)
    order.refresh_from_db()
    assert order_stage(order) == OrderStage.ON_HOLD


def test_holding_an_order_dominates_its_derived_stage(user):
    """A decision about the visit outranks whatever its stones are doing."""
    from apps.gems.enums import OrderHold, OrderStage
    from apps.orders.selectors import order_stage
    from apps.orders.services import hold_order, release_order

    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory())
    assert order_stage(order) == OrderStage.READY_TO_BILL

    hold_order(order, status=OrderHold.ON_HOLD, reason="Customer travelling.", user=user)
    assert order_stage(order) == OrderStage.ON_HOLD
    assert order.held_by == user
    assert order.held_at is not None

    # Releasing returns it exactly where it was - a hold undoes nothing.
    release_order(order, user=user)
    assert order_stage(order) == OrderStage.READY_TO_BILL
    assert order.hold_reason == "", "a stale reason is worse than none"


def test_a_paid_order_cannot_be_cancelled(settings, user):
    """Money has changed hands and there is no refund path in this system."""
    from apps.billing.dev import simulate_payment
    from apps.gems.enums import OrderHold
    from apps.orders.services import hold_order

    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory(category__price=Decimal("1000.00")))
    simulate_payment(generate_bill_for_order(order))
    order.refresh_from_db()

    with pytest.raises(ServiceError, match="cannot be cancelled"):
        hold_order(order, status=OrderHold.CANCELLED, reason="Changed mind.", user=user)

    # Holding it is still allowed - that is the escape hatch the message names.
    hold_order(order, status=OrderHold.ON_HOLD, reason="Refund pending.", user=user)
    assert order.hold_status == OrderHold.ON_HOLD


def test_releasing_an_active_order_is_refused(user):
    """There is nothing to release, so saying so beats a silent no-op."""
    from apps.orders.services import release_order

    order = OrderFactory(stone_count=1)

    with pytest.raises(ServiceError, match="not on hold"):
        release_order(order, user=user)


def test_reception_may_hold_an_order(viewer_user, auth_client):
    """The customer says so at the desk, so reception is who records it."""
    order = OrderFactory(stone_count=1)
    response = auth_client(viewer_user).post(
        f"/api/v1/orders/{order.pk}/hold/",
        {"hold_status": "on_hold", "reason": "Customer travelling."},
    )

    assert response.status_code == 200, response.data


def test_hold_endpoint_requires_the_hold_permission(admin_user, auth_client):
    """Pausing a customer's whole visit is not implied by being able to edit one.

    Gated on its own permission rather than on `change_order`: a custom action
    is a POST, so the method-based map would ask for `add_order` and let anyone
    who can take an order also stop one.
    """
    from django.contrib.auth.models import Permission

    order = OrderFactory(stone_count=1)
    admin_user.user_permissions.add(
        *Permission.objects.filter(
            codename__in=["view_order", "add_order", "change_order"]
        )
    )
    admin_user.groups.clear()

    response = auth_client(admin_user).post(
        f"/api/v1/orders/{order.pk}/hold/",
        {"hold_status": "on_hold", "reason": "Test."},
    )

    assert response.status_code == 403
    order.refresh_from_db()
    assert not order.is_held


def test_hold_endpoint_reports_the_new_stage(admin_user, auth_client):
    """The response is the order as it now reads, so the row updates in place."""
    order = OrderFactory(stone_count=1)
    add_stone(order, stone_type=StoneTypeFactory())

    response = auth_client(admin_user).post(
        f"/api/v1/orders/{order.pk}/hold/",
        {"hold_status": "on_hold", "reason": "Customer travelling."},
    )

    assert response.status_code == 200, response.data
    assert response.data["stage"] == "on_hold"
    assert response.data["stage_label"] == "On hold"
    assert response.data["hold_reason"] == "Customer travelling."
    assert response.data["held_by_label"] is not None


def test_next_stone_label_matches_what_add_stone_allocates():
    """The dialog shows this before the stone exists, so it must not drift.

    A screen that says "this will be stone C" while the service writes "D" is a
    disagreement nobody notices until a customer is holding the paperwork.
    """
    from apps.orders.services import next_stone_label

    order = OrderFactory(stone_count=3)
    stone_type = StoneTypeFactory()

    for expected in ("A", "B", "C"):
        assert next_stone_label(order) == expected
        assert add_stone(order, stone_type=stone_type).label == expected


def test_next_stone_label_is_null_once_the_order_is_full(admin_user, auth_client):
    """Nothing further can be identified, so there is no next label to name."""
    order = OrderFactory(stone_count=1)
    client = auth_client(admin_user)

    before = client.get(f"/api/v1/orders/{order.pk}/")
    assert before.data["next_stone_label"] == "A"

    add_stone(order, stone_type=StoneTypeFactory())

    after = client.get(f"/api/v1/orders/{order.pk}/")
    assert after.data["next_stone_label"] is None


def test_stage_filter_agrees_with_the_derivation(settings, user):
    """The SQL filter and `order_stage` must never disagree.

    The stage is derived, not stored, so filtering means expressing that
    derivation twice - once in Python for display, once in SQL for the queryset.
    Two expressions of one rule is exactly the drift that a stored column was
    rejected to avoid, so it is pinned here: build one order at every stage, then
    assert each filter returns precisely the orders the derivation places there.
    """
    from apps.billing.dev import simulate_payment
    from apps.gems.enums import OrderHold, OrderStage
    from apps.orders.models import Order
    from apps.orders.selectors import order_stage, orders_at_stage
    from apps.orders.services import hold_order

    settings.GEPG_SIMULATE = True
    priced = lambda: StoneTypeFactory(category__price=Decimal("1000.00"))  # noqa: E731

    # empty
    OrderFactory(stone_count=2)

    # identifying — one of two stones typed
    partial = OrderFactory(stone_count=2)
    add_stone(partial, stone_type=priced())

    # ready_to_bill
    ready = OrderFactory(stone_count=1)
    add_stone(ready, stone_type=priced())

    # awaiting_payment
    unpaid = OrderFactory(stone_count=1)
    add_stone(unpaid, stone_type=priced())
    generate_bill_for_order(unpaid)

    # part_paid
    partly = OrderFactory(stone_count=1)
    add_stone(partly, stone_type=priced())
    simulate_payment(generate_bill_for_order(partly), Decimal("400.00"))

    # in_findings — paid, stones not certified
    findings = OrderFactory(stone_count=1)
    add_stone(findings, stone_type=priced())
    simulate_payment(generate_bill_for_order(findings))

    # certified — paid and every stone certified
    certified = OrderFactory(stone_count=2)
    first = add_stone(certified, stone_type=priced())
    second = add_stone(certified, stone_type=priced())
    simulate_payment(generate_bill_for_order(certified))
    transition_stone(first, StoneStatus.CERTIFIED, user=user)
    transition_stone(second, StoneStatus.CERTIFIED, user=user)

    # collected — strictly beyond certified
    collected = OrderFactory(stone_count=1)
    only = add_stone(collected, stone_type=priced())
    simulate_payment(generate_bill_for_order(collected))
    transition_stone(only, StoneStatus.COLLECTED, user=user)

    # on_hold, at the order level
    paused = OrderFactory(stone_count=1)
    add_stone(paused, stone_type=priced())
    hold_order(paused, status=OrderHold.ON_HOLD, reason="Customer away.", user=user)

    # cancelled, at the order level
    dropped = OrderFactory(stone_count=1)
    add_stone(dropped, stone_type=priced())
    hold_order(dropped, status=OrderHold.CANCELLED, reason="Withdrew.", user=user)

    # on_hold via a single parked stone, with the order itself still active
    stone_held = OrderFactory(stone_count=1)
    transition_stone(
        add_stone(stone_held, stone_type=priced()), StoneStatus.ON_HOLD, user=user
    )

    everything = Order.objects.select_related("bill").prefetch_related("stones")
    by_stage: dict[str, set[int]] = {}
    for row in everything:
        by_stage.setdefault(order_stage(row), set()).add(row.pk)

    # Every stage the fixtures produced is reachable, and the filter is exact.
    assert len(by_stage) >= 9, f"fixtures covered only {sorted(by_stage)}"

    for stage in OrderStage.values:
        expected = by_stage.get(stage, set())
        actual = set(orders_at_stage(everything, stage).values_list("pk", flat=True))
        assert actual == expected, (
            f"{stage}: filter returned {sorted(actual)}, "
            f"derivation says {sorted(expected)}"
        )


def test_orders_endpoint_filters_by_stage(settings, admin_user, auth_client):
    """The filter reaches the list endpoint, and paginates like any other."""
    settings.GEPG_SIMULATE = True
    ready = OrderFactory(stone_count=1)
    add_stone(ready, stone_type=StoneTypeFactory(category__price=Decimal("1000.00")))

    unpaid = OrderFactory(stone_count=1)
    add_stone(unpaid, stone_type=StoneTypeFactory(category__price=Decimal("1000.00")))
    generate_bill_for_order(unpaid)

    client = auth_client(admin_user)

    response = client.get("/api/v1/orders/?stage=ready_to_bill")
    assert response.status_code == 200, response.data
    references = {row["reference_number"] for row in response.data["results"]}
    assert format_reference_number(ready.reference_number) in references
    assert format_reference_number(unpaid.reference_number) not in references

    awaiting = client.get("/api/v1/orders/?stage=awaiting_payment")
    assert format_reference_number(unpaid.reference_number) in {
        row["reference_number"] for row in awaiting.data["results"]
    }

    # An unknown stage narrows nothing rather than erroring or returning empty.
    everything = client.get("/api/v1/orders/?stage=not-a-stage")
    assert everything.data["count"] >= 2
