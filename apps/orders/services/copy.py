"""Create an independent copy of an order whose GePG bill is still pending."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import ServiceError
from apps.core.services import generate_order_reference_number, reference_number_for_order
from apps.gems.enums import BillStatus

from ..models import Order, StatusHistory, Stone


def pending_order_copy_available_at(order):
    """Return when this order may be copied, or ``None`` if it is not eligible."""
    bill = getattr(order, "bill", None)
    if (
        bill is None
        or bill.status != BillStatus.PENDING
        or not bill.is_gepg_submitted
        or bill.gepg_submitted_at is None
        or bill.control_number not in ("", "PENDING")
    ):
        return None

    return bill.gepg_submitted_at + timedelta(
        seconds=settings.GEPG_PENDING_ORDER_COPY_AFTER_SECONDS
    )


@transaction.atomic
def copy_pending_order(order: Order, *, user=None) -> Order:
    """Copy an old pending-GePG order and its working data without its bill.

    The new order gets its own reference, stones, findings, and current stone
    states. Billing artifacts and prior history remain attached to the original.
    """
    source = Order.objects.select_for_update().select_related("customer").get(pk=order.pk)
    bill = getattr(source, "bill", None)
    if bill is not None:
        bill = type(bill).objects.select_for_update().get(pk=bill.pk)
        # Use the locked bill state for the eligibility decision.
        source.bill = bill

    available_at = pending_order_copy_available_at(source)
    if available_at is None:
        raise ServiceError(
            "This order no longer has an eligible pending GePG request to copy."
        )
    if timezone.now() < available_at:
        raise ServiceError("Wait until the GePG control-number timeout has elapsed.")

    copied_order = Order(
        reference_number=generate_order_reference_number(),
        customer=source.customer,
        received_date=source.received_date,
        stone_count=source.stone_count,
        hold_status=source.hold_status,
        hold_reason=source.hold_reason,
        held_by=source.held_by,
        held_at=source.held_at,
        created_by=user,
        updated_by=user,
    )
    copied_order.save()

    for source_stone in source.stones.select_related(
        "stone_category", "stone_type"
    ).prefetch_related("reports__instruments_used"):
        copied_stone = Stone(
            order=copied_order,
            label=source_stone.label,
            stone_category=source_stone.stone_category,
            stone_type=source_stone.stone_type,
            weight=source_stone.weight,
            weight_unit=source_stone.weight_unit,
            status=source_stone.status,
            photo=source_stone.photo.name if source_stone.photo else "",
            created_by=user,
            updated_by=user,
        )
        copied_stone.save()
        StatusHistory.objects.create(
            stone=copied_stone,
            from_status="",
            to_status=copied_stone.status,
            changed_by=user,
            note=f"Copied from {source.reference_number}",
        )
        _copy_reports(source_stone, copied_stone, user=user)

    return copied_order


def _copy_reports(source_stone: Stone, copied_stone: Stone, *, user=None) -> None:
    """Copy current findings with fresh report numbers and instrument rows."""
    # Imported here so the orders app does not load another app's models during
    # its own model initialization.
    from apps.identification.models import IdentificationReport, InstrumentUsed

    audit_and_identity_fields = {
        "id",
        "stone",
        "report_number",
        "created_at",
        "updated_at",
        "deleted_at",
        "created_by",
        "updated_by",
        "deleted_by",
    }

    for source_report in source_stone.reports.all():
        values = {
            field.attname: getattr(source_report, field.attname)
            for field in source_report._meta.concrete_fields
            if not field.primary_key and field.name not in audit_and_identity_fields
        }
        copied_report = IdentificationReport(
            **values,
            stone=copied_stone,
            report_number=reference_number_for_order(
                copied_stone.order, "TGC", stone_label=copied_stone.label
            ),
            created_by=user,
            updated_by=user,
        )
        copied_report.save()

        InstrumentUsed.objects.bulk_create(
            [
                InstrumentUsed(
                    report=copied_report,
                    instrument_id=instrument_used.instrument_id,
                    created_by=user,
                    updated_by=user,
                )
                for instrument_used in source_report.instruments_used.all()
            ]
        )
