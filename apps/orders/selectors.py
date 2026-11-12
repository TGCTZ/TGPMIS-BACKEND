"""Reads that encode a workflow gate.

These are the queues the lab actually works from, and each one is a predicate
the whitelist filter backend cannot express - an aggregate, or a comparison
against another column. Keeping them here rather than in a view means the gate
is testable on its own and cannot drift between the screen that lists it and the
service that acts on it.

``billing_worklist`` joins to the bill and therefore lands with ``apps.billing``.
"""

from django.conf import settings
from django.db.models import Count, Exists, F, OuterRef, Q

from apps.gems.enums import BillStatus, OrderHold, OrderStage, StoneStatus

from .models import Order, Stone


def annotate_identified(queryset):
    """Annotate each order with how many of its stones have been identified.

    The alias is ``identified`` rather than ``identified_count``: the latter is
    a property on ``Order``, and an annotation of that name would silently
    shadow it on every row returned.
    """
    return queryset.annotate(identified=Count("stones"))


def identification_worklist():
    """Orders with stones still to identify.

    The customer said how many stones they brought; the bench has typed fewer
    than that so far. Typing a stone is its identification - it is what
    fixes the price, so nothing can be billed until this queue empties.

    """
    queryset = annotate_identified(Order.objects.select_related("customer", "bill"))
    return queryset.filter(identified__lt=F("stone_count"))


def identification_action_queryset():
    """Active, unbilled orders that still have identification work to do.

    Partially identified orders need another stone recorded; fully identified
    orders remain here so staff can correct their typing before billing. Once a
    bill exists, the identification is priced and the order leaves this page.
    Empty orders have no stone action to perform.
    """
    return annotate_identified(
        Order.objects.select_related("customer", "bill").prefetch_related("stones")
    ).filter(bill__isnull=True, stone_count__gt=0)


def order_stage(order) -> str:
    """Where an order has got to, as an :class:`OrderStage` value.

    Derived, never stored. The rule is deliberately pessimistic: an order is
    only as far along as its **least advanced** stone, because a customer
    collecting their stones cares about the last one, not the first. The same
    reasoning makes a single held or cancelled stone dominate the whole order -
    that is the exception worth surfacing in a list.

    Ordering matters. Identification and billing are checked before stone
    statuses, because both are properties of the order as a whole: a partly
    identified order has no stone to be behind, and an unpaid bill outranks
    every stone sitting at ``billed``.

    An order-level hold or cancellation wins over everything else, since it is a
    decision somebody made about the visit rather than a consequence of work.

    Reads ``order.bill`` through the reverse relation rather than importing
    ``apps.billing``, which sits above this module. Callers should
    ``select_related("bill")`` and prefetch stones, or this is a query per row.

    Args:
        order: The order to place.

    Returns:
        One of :class:`~apps.gems.enums.OrderStage`.
    """
    # A decision about the whole visit outranks everything its stones are doing.
    # This is the one part of the stage that is stored rather than derived - see
    # `Order.hold_status`.
    if order.hold_status == OrderHold.CANCELLED:
        return OrderStage.CANCELLED
    if order.hold_status == OrderHold.ON_HOLD:
        return OrderStage.ON_HOLD

    stones = list(order.stones.all())
    if not stones:
        return OrderStage.EMPTY

    statuses = {stone.status for stone in stones}

    # An exception on any *stone* outranks ordinary progress too: these are the
    # rows a supervisor needs to find, and averaging them away would bury them.
    if StoneStatus.CANCELLED in statuses:
        return OrderStage.CANCELLED
    if StoneStatus.ON_HOLD in statuses:
        return OrderStage.ON_HOLD

    if len(stones) < order.stone_count:
        return OrderStage.IDENTIFYING

    bill = getattr(order, "bill", None)
    if bill is None:
        return (
            OrderStage.BILLING_ATTENTION
            if settings.AUTO_BILL_AFTER_IDENTIFICATION
            else OrderStage.READY_TO_BILL
        )
    if (
        settings.AUTO_BILL_AFTER_IDENTIFICATION
        and bill.status == BillStatus.PENDING
        and not bill.control_number
        and bill.status_code not in settings.GEPG_ACK_SUCCESS_CODES
    ):
        return OrderStage.BILLING_ATTENTION
    if bill.status != BillStatus.PAID:
        return OrderStage.AWAITING_PAYMENT

    # Paid, so the bench has the stones. Report the furthest-behind one.
    rank = {
        StoneStatus.COLLECTED: OrderStage.COLLECTED,
        StoneStatus.READY_FOR_COLLECTION: OrderStage.READY_FOR_COLLECTION,
        StoneStatus.CERTIFIED: OrderStage.CERTIFIED,
    }
    order_of_progress = [
        StoneStatus.COLLECTED,
        StoneStatus.READY_FOR_COLLECTION,
        StoneStatus.CERTIFIED,
    ]
    for status in order_of_progress:
        # Every stone at this stage or beyond it.
        if statuses <= {status, *order_of_progress[: order_of_progress.index(status)]}:
            return rank[status]

    return OrderStage.IN_FINDINGS


# The stone statuses each "finished" stage tolerates. An order is *at* a stage
# when every one of its stones has reached it or gone beyond - the same
# least-advanced rule `order_stage` applies, expressed as a set.
_AT_OR_BEYOND = {
    OrderStage.COLLECTED: {StoneStatus.COLLECTED},
    OrderStage.READY_FOR_COLLECTION: {
        StoneStatus.COLLECTED,
        StoneStatus.READY_FOR_COLLECTION,
    },
    OrderStage.CERTIFIED: {
        StoneStatus.COLLECTED,
        StoneStatus.READY_FOR_COLLECTION,
        StoneStatus.CERTIFIED,
    },
}


def _has_stone_at(status) -> Exists:
    """Whether the order has any stone sitting at ``status``."""
    return Exists(Stone.objects.filter(order=OuterRef("pk"), status=status))


def _every_stone_within(statuses) -> Exists:
    """Whether every stone has reached one of ``statuses``.

    Phrased as "no stone is outside the set", which is the only way to ask
    `all()` of a relation in SQL. Negate the result to invert it.
    """
    return Exists(Stone.objects.filter(order=OuterRef("pk")).exclude(status__in=statuses))


def orders_at_stage(queryset, stage: str):
    """Narrow ``queryset`` to the orders whose derived stage is ``stage``.

    The stage is not a column - it is computed by :func:`order_stage` from the
    stones and the bill, deliberately, so that it can never drift out of step
    with them. Filtering therefore means expressing that derivation as SQL
    rather than reading a field.

    A stored ``stage`` column would need every service that touches a stone to
    update it; one forgotten write and an order claims "ready for collection"
    with a stone still on the bench.

    Each branch here mirrors one branch of ``order_stage``, **in the same
    order**, by excluding everything the branches above it would have caught.
    ``test_stage_filter_agrees_with_the_derivation`` asserts the two never
    disagree; change one and that test fails.

    Args:
        queryset: Orders to narrow.
        stage: An :class:`~apps.gems.enums.OrderStage` value.

    Returns:
        The narrowed queryset, or it unchanged if ``stage`` is not a known one.
    """
    if stage not in OrderStage.values:
        return queryset

    held = Q(hold_status=OrderHold.ON_HOLD)
    cancelled = Q(hold_status=OrderHold.CANCELLED)
    exception_stone_cancelled = Q(_has_stone_at(StoneStatus.CANCELLED))
    exception_stone_held = Q(_has_stone_at(StoneStatus.ON_HOLD))

    if stage == OrderStage.CANCELLED:
        return queryset.filter(cancelled | (~held & exception_stone_cancelled))

    if stage == OrderStage.ON_HOLD:
        return queryset.filter(
            held | (~cancelled & ~exception_stone_cancelled & exception_stone_held)
        )

    # Everything past this point is an order nobody has stopped, and none of
    # whose stones is parked.
    running = (
        queryset.filter(
            hold_status=OrderHold.ACTIVE,
            **{},
        )
        .exclude(exception_stone_cancelled)
        .exclude(exception_stone_held)
    )

    counted = annotate_identified(running)

    if stage == OrderStage.EMPTY:
        return counted.filter(identified=0)

    # A stone short of what the customer brought. `stone_count` of 0 with no
    # stones is EMPTY, caught above.
    has_stones = counted.filter(identified__gt=0)

    if stage == OrderStage.IDENTIFYING:
        return has_stones.filter(identified__lt=F("stone_count"))

    full = has_stones.filter(identified__gte=F("stone_count"))

    if stage == OrderStage.READY_TO_BILL:
        if settings.AUTO_BILL_AFTER_IDENTIFICATION:
            return full.none()
        return full.filter(bill__isnull=True)

    if stage == OrderStage.BILLING_ATTENTION:
        if not settings.AUTO_BILL_AFTER_IDENTIFICATION:
            return full.none()
        return full.filter(
            Q(bill__isnull=True)
            | (
                Q(bill__control_number="")
                & Q(bill__status=BillStatus.PENDING)
                & ~Q(bill__status_code__in=settings.GEPG_ACK_SUCCESS_CODES)
            )
        )

    billed = full.filter(bill__isnull=False)

    if settings.AUTO_BILL_AFTER_IDENTIFICATION:
        billed = billed.exclude(
            Q(bill__control_number="")
            & Q(bill__status=BillStatus.PENDING)
            & ~Q(bill__status_code__in=settings.GEPG_ACK_SUCCESS_CODES)
        )

    if stage == OrderStage.AWAITING_PAYMENT:
        return billed.exclude(bill__status=BillStatus.PAID)

    paid = billed.filter(bill__status=BillStatus.PAID)

    if stage in _AT_OR_BEYOND:
        within = ~Q(_every_stone_within(_AT_OR_BEYOND[stage]))
        # Exclude the stages above this one, which are strictly more advanced.
        beyond = Q()
        for statuses in _AT_OR_BEYOND.values():
            if len(statuses) < len(_AT_OR_BEYOND[stage]):
                beyond |= ~Q(_every_stone_within(statuses))
        return paid.filter(within).exclude(beyond)

    # IN_FINDINGS: paid, but some stone has not yet reached certified.
    return paid.filter(_every_stone_within(_AT_OR_BEYOND[OrderStage.CERTIFIED]))
