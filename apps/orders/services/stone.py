"""Identification of stones, and status transitions."""

from django.conf import settings
from django.db import transaction

from apps.core.exceptions import ServiceError
from apps.core.services import format_reference_number
from apps.gems.enums import StoneStatus
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify_subscribers

from ..models import Order, StatusHistory, Stone


def transition_stone(stone: Stone, to_status: str, *, user=None, note: str = "") -> Stone:
    """Move a stone to a new status and record it in the audit trail.

    No-ops if the stone is already in ``to_status``, so a redelivered payment
    notification does not write a duplicate history row.

    The service accepts any target status; callers determine the usual sequence.

    Args:
        stone: The stone to move.
        to_status: A ``StoneStatus`` value.
        user: The acting user, or None when the gateway is the actor.
        note: Free text explaining the move, shown in the history.
    """
    if stone.status == to_status:
        return stone

    from_status = stone.status
    stone.status = to_status
    if user is not None:
        stone.updated_by = user
    stone.save(update_fields=["status", "updated_at", "updated_by"])

    StatusHistory.objects.create(
        stone=stone,
        from_status=from_status,
        to_status=to_status,
        changed_by=user,
        note=note,
    )
    return stone


def next_stone_label(order) -> str:
    """The label the next stone identified against this order will carry.

    New orders use ``1``, ``2``, ``3``. An order with existing alphabetic stone
    labels keeps that scheme, including spreadsheet-style labels after ``Z``;
    this preserves the identity of work already attached to legacy labels.

    Extracted so the screen can *show* the label before it is allocated rather
    than reimplementing the rule - a dialog that promises one label while the
    service writes another is the kind of disagreement nobody notices until a
    customer is holding the paperwork.

    Args:
        order: The order the stone would belong to.
    """
    existing_labels = Stone.all_objects.filter(order=order).values_list(
        "label", flat=True
    )
    has_legacy_letters = any(label.isalpha() for label in existing_labels)
    next_number = order.stones.count() + 1
    if not has_legacy_letters:
        return str(next_number)

    # Excel-style labels keep legacy orders readable if one ever exceeds 26
    # stones, while new orders no longer depend on a finite alphabet.
    label = ""
    while next_number:
        next_number, remainder = divmod(next_number - 1, 26)
        label = chr(65 + remainder) + label
    return label


@transaction.atomic
def add_stone(order: Order, *, stone_category=None, stone_type=None, user=None) -> Stone:
    """Record a stone's pricing category; its exact type is found later.

    ``stone_type`` remains accepted for older callers. When supplied without a
    category, its category is derived so old clients can continue to identify
    stones while the frontend rolls out.

    New orders receive numeric labels. Existing orders with alphabetic labels
    continue that sequence. The order cannot hold more stones than the customer
    said they brought.

    Args:
        order: The order the stone belongs to.
        stone_category: The ``gems.StoneCategory`` used to price this stone.
        stone_type: Optional legacy type; its category is derived if omitted.
        user: The acting user.

    Raises:
        ServiceError: If the category is missing/mismatched or the order is full.
    """
    if stone_category is None and stone_type is not None:
        stone_category = stone_type.category
    if stone_category is None:
        raise ServiceError("Select a stone category.")
    if stone_type is not None and stone_type.category_id != stone_category.pk:
        raise ServiceError("Choose a type belonging to this category.")

    identified = order.stones.count()
    if identified >= order.stone_count:
        raise ServiceError(
            f"All {order.stone_count} stone(s) for "
            f"{format_reference_number(order.reference_number)} have "
            f"already been identified."
        )

    label = next_stone_label(order)

    stone = Stone(
        order=order,
        label=label,
        stone_category=stone_category,
        stone_type=stone_type,
        status=StoneStatus.RECEIVED,
    )
    if user is not None:
        stone.created_by = user
    stone.save()

    StatusHistory.objects.create(
        stone=stone,
        to_status=StoneStatus.RECEIVED,
        changed_by=user,
        note="Identified",
    )

    # Only the stone that completes the order: the bill prices every stone at
    # once, so accounts has nothing to act on until the last one is typed.
    if (
        identified + 1 == order.stone_count
        and not settings.AUTO_BILL_AFTER_IDENTIFICATION
    ):
        notify_subscribers(
            NotificationKind.READY_TO_BILL,
            title=(
                f"Order {format_reference_number(order.reference_number)} "
                "is ready to bill"
            ),
            body=f"All {order.stone_count} stone(s) have been identified.",
            link="/bills?source=waiting",
            exclude=user,
        )
    return stone


# The statuses in which a stone's *pricing category* may still be corrected.
#
# ``received`` is the working state. ``on_hold`` and ``cancelled`` are the two a
# human parks a stone in precisely *to* fix something, so they stay open. Every
# other status means a bill has been priced from this stone's category, so
# changing it afterwards would silently make an issued bill wrong.
#
# **This covers the type, not the whole record.** Weight arrives later than
# billing by design: the bench weighs the stone during the findings, when it is
# already ``paid``. Locking the row outright would make that impossible.
RETYPEABLE_STATUSES = frozenset(
    {StoneStatus.RECEIVED, StoneStatus.ON_HOLD, StoneStatus.CANCELLED}
)


def assert_stone_retypeable(stone: Stone) -> None:
    """Refuse a category change once a bill has been priced from the current one.

    Raises:
        ServiceError: If the stone's status is not in ``RETYPEABLE_STATUSES``.
    """
    if stone.status not in RETYPEABLE_STATUSES:
        raise ServiceError(
            f"{stone.label} is {stone.get_status_display().lower()}, so its "
            "category can no longer change - it priced the bill."
        )


# ``None`` is a meaningful weight - it is what a stone has before the bench
# weighs it, and what a gemmologist sets after realising they typed a reading
# against the wrong stone. A ``None`` default could not tell "clear this" from
# "leave this alone", so an omitted weight gets its own sentinel.
_UNSET = object()


def update_stone(
    stone: Stone,
    *,
    stone_category=_UNSET,
    stone_type=_UNSET,
    weight=_UNSET,
    weight_unit=None,
    photo=_UNSET,
    user=None,
) -> Stone:
    """Update a stone's recorded properties during the findings stage.

    ``photo`` uses the same ``_UNSET`` sentinel as ``weight`` so that passing
    ``None`` clears the image, while omitting it leaves the existing one alone -
    a plain default of ``None`` would silently wipe the photograph on every
    partial update that did not mention it.

    Raises:
        ServiceError: If a billed stone's category changes, if automatic billing
            locks an existing classification, if its type belongs to another
            category, or if a finalized report's type changes.
    """
    classification_changed = (
        stone_category is not _UNSET
        and (stone_category.pk if stone_category is not None else None)
        != stone.stone_category_id
    ) or (
        stone_type is not _UNSET
        and (stone_type.pk if stone_type is not None else None) != stone.stone_type_id
    )
    if (
        settings.AUTO_BILL_AFTER_IDENTIFICATION
        and stone.stone_type_id is not None
        and classification_changed
    ):
        raise ServiceError(
            "A saved identification cannot be changed while automatic billing is enabled."
        )

    report = stone.report
    if report is not None and report.is_finalized:
        if user is None or not user.has_perm("identification.edit_finalized_report"):
            raise ServiceError("A finalized report's stone cannot be edited.")
        if stone_category is not _UNSET and stone_category.pk != stone.stone_category_id:
            raise ServiceError("A finalized report's stone category cannot change.")

    if stone_category is not _UNSET and stone_category.pk != stone.stone_category_id:
        assert_stone_retypeable(stone)
        stone.stone_category = stone_category
        if (
            stone_type is _UNSET
            and stone.stone_type_id
            and stone.stone_type.category_id != stone_category.pk
        ):
            # A previous type was valid for the old tier; after reclassifying
            # the tier, require the bench to record a matching exact type.
            stone.stone_type = None

    if stone_type is not _UNSET:
        if stone_type is not None:
            if stone_type.category_id != stone.stone_category_id:
                raise ServiceError("Choose a type belonging to this category.")
            if (
                report is not None
                and report.is_finalized
                and not user.has_perm("identification.edit_finalized_report")
            ):
                raise ServiceError("A finalized report's stone type cannot change.")
        elif (
            report is not None
            and report.is_finalized
            and not user.has_perm("identification.edit_finalized_report")
        ):
            raise ServiceError("A finalized report's stone type cannot change.")
        stone.stone_type = stone_type
    if weight is not _UNSET:
        stone.weight = weight
    if weight_unit:
        stone.weight_unit = weight_unit
    if photo is not _UNSET:
        stone.photo = photo
    if user is not None:
        stone.updated_by = user

    stone.save(
        update_fields=[
            "stone_category",
            "stone_type",
            "weight",
            "weight_unit",
            "photo",
            "updated_at",
            "updated_by",
        ]
    )
    return stone
