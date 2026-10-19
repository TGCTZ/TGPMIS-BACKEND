"""Order intake, and holding a whole order."""

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import ServiceError
from apps.core.services import (
    format_reference_number,
    generate_order_reference_number,
)
from apps.gems.enums import BillStatus, OrderHold
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify_subscribers

from ..models import Customer, Order


@transaction.atomic
def create_order(
    *,
    customer=None,
    customer_data: dict | None = None,
    stone_count: int,
    received_date=None,
    user=None,
) -> Order:
    """Register an order and how many stones the customer submitted.

    Reception records only the count. The individual stones, with the type that
    prices them, are identified afterwards via ``add_stone`` - which is what
    stops an order from quietly holding more stones than were handed over.

    A customer arrives one of two ways: ``customer`` for somebody already on
    file, or ``customer_data`` to register them now. Reception meets most
    customers for the first time *because* an order is being received, so
    demanding they exist beforehand would be a step that serves nobody. Creating
    them inside this transaction is what keeps a failed order from leaving an
    orphan customer behind.

    Args:
        customer: The submitting customer, if already on file.
        customer_data: Validated fields for a customer to register now.
        stone_count: How many stones were handed over.
        received_date: Defaults to today.
        user: The acting user.

    Raises:
        ValueError: If neither a customer nor customer_data was given.
    """
    if customer is None:
        if not customer_data:
            raise ValueError("create_order needs a customer or customer_data.")
        customer = Customer(**customer_data)
        if user is not None:
            customer.created_by = user
        customer.save()

    order = Order(
        reference_number=generate_order_reference_number(),
        customer=customer,
        received_date=received_date or timezone.now().date(),
        stone_count=stone_count,
    )
    if user is not None:
        order.created_by = user
    order.save()

    notify_subscribers(
        NotificationKind.ORDER_RECEIVED,
        title=f"New order {format_reference_number(order.reference_number)}",
        body=f"{stone_count} stone(s) from {customer} awaiting identification.",
        link="/identification?source=waiting",
        exclude=user,
    )
    return order


def hold_order(order: Order, *, status: str, reason: str = "", user=None) -> Order:
    """Pause or withdraw a whole order.

    The one piece of an order's state that is stored rather than derived,
    because no stone can express it: "the customer asked us to stop" is a fact
    about the visit. Once set it dominates the derived stage, so the row reads
    as held wherever it appears.

    **Cancelling a paid order is refused.** Money has changed hands and this
    system has no refund path, so a cancelled-but-paid order would be a record
    nobody could act on. Hold it instead and settle the refund outside the
    system.

    Nothing is undone here. Stones keep their own statuses, the bill stays as it
    is, and releasing the hold returns the order to exactly where it was - which
    is the point of a hold as opposed to a deletion.

    Args:
        order: The order to hold.
        status: ``OrderHold.ON_HOLD`` or ``OrderHold.CANCELLED``.
        reason: Why, in the words of whoever decided it.
        user: Who decided.

    Raises:
        ServiceError: If ``status`` is not a hold, or the order is paid and the
            caller is trying to cancel it.
    """
    if status not in (OrderHold.ON_HOLD, OrderHold.CANCELLED):
        raise ServiceError("Use release_order to return an order to active.")

    if status == OrderHold.CANCELLED:
        bill = getattr(order, "bill", None)
        if bill is not None and bill.status == BillStatus.PAID:
            raise ServiceError(
                f"{format_reference_number(order.reference_number)} has been paid "
                "and cannot be cancelled. "
                "Put it on hold instead."
            )

    order.hold_status = status
    order.hold_reason = reason
    order.held_by = user
    order.held_at = timezone.now()
    if user is not None:
        order.updated_by = user
    order.save(
        update_fields=[
            "hold_status",
            "hold_reason",
            "held_by",
            "held_at",
            "updated_at",
            "updated_by",
        ]
    )
    return order


def release_order(order: Order, *, user=None) -> Order:
    """Return a held or cancelled order to active work.

    Clears the reason as well as the flag: a stale "customer travelling" against
    an order back in the queue is worse than no reason at all. The audit log
    keeps what it said.

    Raises:
        ServiceError: If the order is not held.
    """
    if order.hold_status == OrderHold.ACTIVE:
        raise ServiceError(
            f"{format_reference_number(order.reference_number)} is not on hold."
        )

    order.hold_status = OrderHold.ACTIVE
    order.hold_reason = ""
    order.held_by = None
    order.held_at = None
    if user is not None:
        order.updated_by = user
    order.save(
        update_fields=[
            "hold_status",
            "hold_reason",
            "held_by",
            "held_at",
            "updated_at",
            "updated_by",
        ]
    )
    return order
