"""Development-only helpers to simulate GePG callbacks.

Never wired into production paths. Used by the dev-only "Simulate payment"
action and by ``manage.py seed`` so the full order → bill → payment →
certificate flow can be exercised without the real gateway.
"""

import secrets
from decimal import Decimal
from xml.sax.saxutils import escape

from django.utils import timezone

from .models import Bill
from .services import process_payment_notification


def _fake_control_number() -> str:
    """A plausible 12-digit GePG control number."""
    return f"99{secrets.randbelow(10**10):010d}"


def _payment_xml(bill: Bill, amount: Decimal, trx_id: str) -> str:
    """Build a pmtSpNtfReq payload matching the real GePG notification format."""
    customer = bill.order.customer
    now = timezone.now().strftime("%Y-%m-%dT%H:%M:%S")
    return (
        "<Gepg><pmtSpNtfReq>"
        "<PmtHdr>"
        f"<ReqId>SIM-{trx_id}</ReqId>"
        f"<GrpBillId>{bill.bill_number}</GrpBillId>"
        "<SpGrpCode>SIM</SpGrpCode>"
        f"<CustCntrNum>{bill.control_number}</CustCntrNum>"
        "<EntryCnt>1</EntryCnt>"
        "</PmtHdr>"
        "<PmtDtls><PmtTrxDtl>"
        "<SpCode>SIM</SpCode>"
        f"<BillId>{bill.bill_number}</BillId>"
        f"<BillCtrNum>{bill.control_number}</BillCtrNum>"
        "<PspCode>SIM</PspCode><PspName>Simulated (dev)</PspName>"
        f"<TrxId>{trx_id}</TrxId><PayRefId>SIM-REF</PayRefId>"
        f"<BillAmt>{amount}</BillAmt><PaidAmt>{amount}</PaidAmt>"
        f"<BillPayOpt>3</BillPayOpt><Ccy>{bill.currency}</Ccy>"
        "<CollAccNum>SIM-ACC</CollAccNum>"
        f"<TrxDtTm>{now}</TrxDtTm><UsdPayChnl>SIMULATION</UsdPayChnl>"
        f"<PyrCellNum>{escape(customer.phone)}</PyrCellNum>"
        f"<PyrName>{escape(customer.full_name)}</PyrName>"
        f"<PyrEmail>{escape(customer.email)}</PyrEmail>"
        "</PmtTrxDtl></PmtDtls>"
        "</pmtSpNtfReq><signature>simulated</signature></Gepg>"
    )


def simulate_payment(bill: Bill) -> Bill:
    """Pay a bill by feeding a fake GePG notification through the real handler.

    Assigns a control number first if the (offline) gateway never returned one, so the
    bill is payable. Exercises the real ``process_payment_notification`` path — it
    records a ``Payment``, settles the bill, and transitions the stones.

    Args:
        bill: The bill to pay in full.

    Returns:
        The bill, refreshed. Each call writes a new transaction id, so paying
        twice tops a bill up rather than being swallowed as a redelivery.
    """
    if not bill.control_number:
        bill.control_number = _fake_control_number()
        bill.save(update_fields=["control_number", "updated_at"])

    paid = sum(
        (payment.paid_amount or Decimal("0") for payment in bill.payments.all()),
        Decimal("0"),
    )
    amount = max(Decimal("0"), bill.total_amount - paid)

    trx_id = f"SIM-{secrets.token_hex(6).upper()}"
    process_payment_notification(_payment_xml(bill, amount, trx_id))
    bill.refresh_from_db()
    return bill
