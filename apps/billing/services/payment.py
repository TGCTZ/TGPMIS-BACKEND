"""Payment notifications received from GePG."""

import logging
from decimal import Decimal
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException

from django.db import transaction

from apps.core.exceptions import ServiceError
from apps.core.services import format_reference_number
from apps.gems.enums import BillStatus, StoneStatus
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify_subscribers
from apps.orders.services import transition_stone

from ..gateways.gepg import build_payment_ack, parse_payment_notification
from ..models import Bill, BillItem, Payment

logger = logging.getLogger(__name__)


def process_payment_notification(xml_content: str) -> str:
    """Handle a GePG payment notification callback; return a signed ack XML.

    Every exit path returns well-formed XML, because the gateway is not a
    browser: an unparseable body or a failure mid-way must return a protocol
    acknowledgement code rather than an HTML error page.
    """
    try:
        header, transactions = parse_payment_notification(xml_content)
    except (ValueError, ParseError, DefusedXmlException) as exc:
        logger.error("Invalid GePG payment notification: %s", exc)
        return build_payment_ack("ERROR", "7242")

    req_id = header["req_id"]
    try:
        for txn in transactions:
            _apply_payment(header, txn, xml_content)
    except ServiceError as exc:
        logger.info("GePG payment notification needs bill reconciliation: %s", exc)
        return build_payment_ack(req_id or "ERROR", "7204")
    except Exception:
        logger.exception("Failed to process GePG payment notification")
        return build_payment_ack(req_id or "ERROR", "7321")
    return build_payment_ack(req_id, "7101")


@transaction.atomic
def _apply_payment(header: dict, txn: dict, raw: str) -> None:
    """Record one payment transaction and settle its bill if fully paid.

    Idempotent on ``trx_id``: GePG redelivers a notification until it is
    acknowledged, so a repeat updates the stored row and returns without
    recomputing the settlement - otherwise a redelivery would re-transition
    every stone and write a second history entry each time.
    """
    bill = (
        Bill.all_objects.select_for_update()
        .filter(bill_number=txn["gepg_bill_id"])
        .first()
        or Bill.all_objects.select_for_update()
        .filter(control_number=txn["bill_ctr_num"])
        .first()
    )
    if bill is None:
        raise ServiceError(f"Bill '{txn['gepg_bill_id']}' not found.")

    # A notification can be in flight while an administrator deletes a bill.
    # Retain the payment and restore its context instead of orphaning a real
    # gateway transaction behind the soft-delete manager.
    if bill.is_deleted:
        bill.restore()
        for item in BillItem.all_objects.filter(bill=bill, deleted_at__isnull=False):
            item.restore()

    defaults = {**header, **txn, "bill": bill, "is_processed": True, "raw_request": raw}
    defaults.pop("trx_id", None)
    payment, created = Payment.objects.get_or_create(
        trx_id=txn["trx_id"], defaults=defaults
    )
    if not created:
        for field, value in defaults.items():
            setattr(payment, field, value)
        payment.save()
        return

    paid = sum((p.paid_amount or Decimal("0") for p in bill.payments.all()), Decimal("0"))
    if paid >= bill.total_amount:
        bill.status = BillStatus.PAID
        # The gateway is the actor here, so there is no user to attribute.
        for stone in bill.order.stones.all():
            transition_stone(stone, StoneStatus.PAID, note="Bill settled via GePG")
        notify_subscribers(
            NotificationKind.BILL_PAID,
            title=(
                f"Order {format_reference_number(bill.order.reference_number)} "
                "has been paid"
            ),
            body=(
                f"Bill {format_reference_number(bill.bill_number)} is settled; "
                "findings can now be recorded."
            ),
            link="/identification-reports?source=waiting",
        )
    else:
        bill.status = BillStatus.PARTIALLY_PAID
    bill.save(update_fields=["status", "updated_at"])
