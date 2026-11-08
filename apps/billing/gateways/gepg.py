"""GePG gateway: build/submit bill XML and parse payment notifications.

Isolates the GePG wire format (XML, HTTP, status codes) from the service layer.
"""

import base64
import logging
import re
import secrets
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

import requests
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as parse_xml

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..constants import CURRENCY_TZS
from ..gepg_transport import finish_gepg_request, send_gepg_request
from ..models import GePGRequest
from .signing import sign_if_enabled

logger = logging.getLogger(__name__)

ACK_SUCCESS = settings.GEPG_ACK_SUCCESS_CODES


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def format_amount(value) -> str:
    """Format a money value to a 2-decimal string."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:.2f}"


def escape_xml(text) -> str:
    """Escape XML special characters in a value."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def normalize_msisdn(phone) -> str:
    """Normalize a phone number to Tanzania MSISDN format (255XXXXXXXXX)."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("255"):
        normalized = digits
    elif digits.startswith("0") and len(digits) >= 10:
        normalized = f"255{digits[1:]}"
    elif len(digits) == 9:
        normalized = f"255{digits}"
    else:
        normalized = digits
    return normalized[:12]


def generate_req_id() -> str:
    """Create a collision-resistant ID while retaining the provider prefix."""
    prefix = settings.GEPG_SP_GRP_CODE[:20]
    return f"{prefix}{uuid4().hex}"


def parse_datetime(value: str) -> datetime:
    """Parse a GePG datetime (tolerating '.'/':' fractional seconds) as aware."""
    if not value:
        raise ValueError("Missing datetime value")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        normalised = re.sub(r":(\d{1,6})$", r".\1", value)
        dt = datetime.strptime(normalised, "%Y-%m-%dT%H:%M:%S.%f")
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


# ------------------------------------------------------------
# Outbound: bill submission
# ------------------------------------------------------------
def build_bill_xml(bill, customer, username: str = "System") -> str:
    """Build the (unsigned) billSubReq XML for a bill and its customer."""
    amount = format_amount(bill.total_amount)
    gen_at = bill.issued_at or timezone.now()
    expiry = bill.expiry_at or gen_at
    phone = normalize_msisdn(customer.phone)
    cust_id = re.sub(r"\D", "", str(customer.id)).zfill(8)[:20]

    items_xml = ""
    for item in bill.items.all():
        items_xml += f"""
          <BillItem>
            <RefBillId>{bill.bill_number}</RefBillId>
            <SubSpCode>{settings.GEPG_SUB_SP_CODE}</SubSpCode>
            <GfsCode>{settings.GEPG_GFS_CODE}</GfsCode>
            <BillItemRef>{escape_xml(item.item_ref or f"ITEM-{item.id}")}</BillItemRef>
            <UseItemRefOnPay>N</UseItemRefOnPay>
            <BillItemAmt>{format_amount(item.amount)}</BillItemAmt>
            <BillItemEqvAmt>{format_amount(item.amount)}</BillItemEqvAmt>
            <CollSp>{settings.GEPG_SP_GRP_CODE}</CollSp>
          </BillItem>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Gepg>
  <billSubReq>
    <BillHdr>
      <ReqId>{generate_req_id()}</ReqId>
      <SpGrpCode>{settings.GEPG_SP_GRP_CODE}</SpGrpCode>
      <SysCode>{settings.GEPG_SYS_CODE}</SysCode>
      <BillTyp>{bill.bill_type}</BillTyp>
      <PayTyp>{bill.pay_type}</PayTyp>
      <GrpBillId>{bill.bill_number}</GrpBillId>
    </BillHdr>
    <BillDtls>
      <BillDtl>
        <BillId>{bill.bill_number}</BillId>
        <SpCode>{settings.GEPG_SP_CODE}</SpCode>
        <CollCentCode>{settings.GEPG_COLL_CENT_CODE}</CollCentCode>
        <BillDesc>{escape_xml(f"TGC bill {bill.bill_number}")}</BillDesc>
        <CustTin>000000000</CustTin>
        <CustId>{cust_id}</CustId>
        <CustIdTyp>5</CustIdTyp>
        <CustAccnt>TGCACCNT</CustAccnt>
        <CustName>{escape_xml(customer.full_name)}</CustName>
        <CustCellNum>{phone}</CustCellNum>
        <CustEmail>{escape_xml(customer.email)}</CustEmail>
        <BillGenDt>{gen_at:%Y-%m-%dT%H:%M:%S}</BillGenDt>
        <BillExprDt>{expiry:%Y-%m-%dT%H:%M:%S}</BillExprDt>
        <BillGenBy>{escape_xml(username)}</BillGenBy>
        <BillApprBy>{escape_xml(username)}</BillApprBy>
        <BillAmt>{amount}</BillAmt>
        <BillEqvAmt>{amount}</BillEqvAmt>
        <MinPayAmt>{amount}</MinPayAmt>
        <Ccy>{bill.currency}</Ccy>
        <ExchRate>1.00</ExchRate>
        <BillPayOpt>3</BillPayOpt>
        <PayPlan>1</PayPlan>
        <PayLimTyp>1</PayLimTyp>
        <PayLimAmt>0.00</PayLimAmt>
        <CollPsp></CollPsp>
        <BillItems>{items_xml}
        </BillItems>
      </BillDtl>
    </BillDtls>
  </billSubReq>
  <signature>SignatureGoesHere</signature>
</Gepg>"""


def submit_bill(bill, customer, username: str = "System") -> dict:
    """Sign and POST a bill to GePG; return the parsed outcome.

    Result keys: success, control_number, status_code, status_desc, is_sync,
    raw_response.
    """
    if getattr(settings, "GEPG_SIMULATE", False):
        # Development: skip the network and return an instant control number.
        control_number = f"99{secrets.randbelow(10**10):010d}"
        return _result(True, control_number, "7101", "Simulated (dev)", True, None)

    xml_payload = sign_if_enabled(build_bill_xml(bill, customer, username))
    request_record, raw = send_gepg_request(
        GePGRequest.Operation.BILL,
        settings.GEPG_BILL_CREATE_URL,
        xml_payload,
        bill=bill,
    )
    if raw is None:
        logger.error("GePG bill request %s will be retried", request_record.req_id)
        return _result(
            False,
            None,
            "CONNECTION_ERROR",
            request_record.last_error,
            False,
            None,
        )
    result = _parse_bill_response(raw)
    finish_gepg_request(
        request_record,
        accepted=result["success"],
        status_code=result["status_code"],
        status_desc=result["status_desc"],
        response_xml=raw,
    )
    return result


@transaction.atomic
def send_control_number_sms(bill) -> bool:
    """Send the old system's Beem SMS once after a control number is available."""
    bill = bill.__class__.objects.select_for_update().get(pk=bill.pk)
    if (
        settings.GEPG_SIMULATE
        or not bill.control_number
        or bill.control_number == "PENDING"
        or bill.control_number_sms_sent_at
    ):
        return False
    api_key = getattr(settings, "BEEM_AFRICA_API_KEY", "")
    secret = getattr(settings, "BEEM_AFRICA_SECRET_KEY", "")
    phone = normalize_msisdn(bill.order.customer.phone)
    if not api_key or not secret or not phone:
        return False
    payload = {
        "source_addr": "TGC-INFO",
        "schedule_time": "",
        "encoding": 0,
        "message": (
            f"Pay TZS {bill.total_amount:,.0f} to the control number "
            f"{bill.control_number}"
        ),
        "recipients": [{"recipient_id": 1, "dest_addr": phone}],
    }
    response = requests.post(
        "https://apisms.beem.africa/v1/send",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic "
            + base64.b64encode(f"{api_key}:{secret}".encode()).decode(),
        },
        timeout=30,
    )
    if response.status_code != 200:
        logger.warning(
            "Beem SMS failed for bill %s with HTTP %s",
            bill.bill_number,
            response.status_code,
        )
        return False
    bill.control_number_sms_sent_at = timezone.now()
    bill.save(update_fields=["control_number_sms_sent_at", "updated_at"])
    return True


def _parse_bill_response(raw: str) -> dict:
    """Parse a billSubRes (sync control number) or billSubReqAck (async) response."""
    try:
        root = parse_xml(raw)
    except (ParseError, DefusedXmlException) as exc:
        logger.error("GePG response parse error: %s", exc)
        return _result(False, None, "XML_PARSE_ERROR", str(exc), False, raw)

    bill_dtl = root.find("billSubRes//BillDtl")
    if bill_dtl is not None:
        control_number = bill_dtl.findtext("BillCntrNum")
        status_code = bill_dtl.findtext("BillStsCode") or root.findtext(
            "billSubRes/BillHdr/ResStsCode", ""
        )
        if control_number in (None, "", "0") or status_code != "7101":
            control_number = None
        return _result(
            bool(control_number),
            control_number,
            status_code,
            bill_dtl.findtext("BillStsDesc", ""),
            True,
            raw,
        )

    ack = root.find("billSubReqAck")
    if ack is not None:
        code = ack.findtext("AckStsCode", "")
        desc = ack.findtext("AckStsDesc", "")
        if code in ACK_SUCCESS:
            # Acknowledged; control number will arrive via callback.
            return _result(
                True, "PENDING", code, desc or "Awaiting control number", False, raw
            )
        return _result(False, None, code, desc or "Rejected by GePG", False, raw)

    return _result(
        False, None, "UNKNOWN_RESPONSE", "Unrecognised GePG response", False, raw
    )


def _result(success, control_number, status_code, status_desc, is_sync, raw) -> dict:
    return {
        "success": success,
        "control_number": control_number,
        "status_code": status_code,
        "status_desc": status_desc,
        "is_sync": is_sync,
        "raw_response": raw,
    }


# ------------------------------------------------------------
# Inbound: payment notification
# ------------------------------------------------------------
def parse_payment_notification(xml_content: str) -> tuple[dict, list[dict]]:
    """Parse a pmtSpNtfReq into (header, [transactions]). Raises ValueError if invalid."""
    root = parse_xml(xml_content)
    pmt_hdr = root.find(".//PmtHdr")
    details = root.findall(".//PmtTrxDtl")
    if pmt_hdr is None or not details:
        raise ValueError("Missing payment header or details")

    header = {
        "req_id": pmt_hdr.findtext("ReqId", ""),
        "grp_bill_id": pmt_hdr.findtext("GrpBillId", ""),
        "sp_grp_code": pmt_hdr.findtext("SpGrpCode", ""),
        "cust_cntr_num": pmt_hdr.findtext("CustCntrNum", ""),
        "entry_count": int(pmt_hdr.findtext("EntryCnt", "1")),
    }
    if (
        not header["req_id"]
        or not header["grp_bill_id"]
        or not header["sp_grp_code"]
        or not header["cust_cntr_num"]
        or header["entry_count"] != len(details)
    ):
        raise ValueError("Payment notification header or EntryCnt is invalid")
    transactions = [parse_payment_detail(detail) for detail in details]
    return header, transactions


def parse_payment_detail(detail) -> dict:
    """Translate a payment detail into the shared payment persistence shape."""
    required = (
        "SpCode",
        "BillId",
        "BillCtrNum",
        "PspCode",
        "PspName",
        "TrxId",
        "PayRefId",
        "BillAmt",
        "PaidAmt",
        "BillPayOpt",
        "Ccy",
        "CollAccNum",
        "TrxDtTm",
        "UsdPayChnl",
    )
    if any(not detail.findtext(name) for name in required):
        raise ValueError("Payment transaction is missing a required field")
    currency = detail.findtext("Ccy", "")
    if currency != CURRENCY_TZS:
        raise ValueError(f"Unsupported payment currency: {currency}")
    return {
        "cust_cntr_num": detail.findtext("CustCntrNum", ""),
        "sp_code": detail.findtext("SpCode", ""),
        "gepg_bill_id": detail.findtext("BillId", ""),
        "bill_ctr_num": detail.findtext("BillCtrNum", ""),
        "psp_code": detail.findtext("PspCode", ""),
        "psp_name": detail.findtext("PspName", ""),
        "trx_id": detail.findtext("TrxId", ""),
        "pay_ref_id": detail.findtext("PayRefId", ""),
        "bill_amount": Decimal(detail.findtext("BillAmt", "0")),
        "paid_amount": Decimal(detail.findtext("PaidAmt", "0")),
        "bill_pay_opt": detail.findtext("BillPayOpt", ""),
        "currency": currency,
        "coll_acc_num": detail.findtext("CollAccNum", ""),
        "trx_dt_tm": parse_datetime(detail.findtext("TrxDtTm", "")),
        "usd_pay_chnl": detail.findtext("UsdPayChnl", ""),
        "pyr_cell_num": detail.findtext("PyrCellNum", ""),
        "pyr_email": detail.findtext("PyrEmail", ""),
        "pyr_name": detail.findtext("PyrName", ""),
    }


def build_payment_ack(req_id: str, status_code: str = "7101") -> str:
    """Build a signed pmtSpNtfReqAck response for GePG."""
    ack = f"""<?xml version="1.0" encoding="UTF-8"?>
<Gepg>
    <pmtSpNtfReqAck>
        <AckId>{generate_req_id()}</AckId>
        <ReqId>{escape_xml(req_id)}</ReqId>
        <AckStsCode>{status_code}</AckStsCode>
    </pmtSpNtfReqAck>
    <signature>SignatureGoesHere</signature>
</Gepg>"""
    return sign_if_enabled(ack)


# ------------------------------------------------------------
# Inbound: async control-number callback (billSubRes)
# ------------------------------------------------------------
def parse_bill_response(xml_content: str) -> dict:
    """Parse an async billSubRes callback into its control-number fields."""
    root = parse_xml(xml_content)
    hdr = root.find("billSubRes/BillHdr")
    dtl = root.find("billSubRes//BillDtl")
    if dtl is None:
        raise ValueError("Missing BillDtl in billSubRes")
    return {
        "req_id": hdr.findtext("ReqId", "") if hdr is not None else "",
        "res_id": hdr.findtext("ResId", "") if hdr is not None else "",
        "bill_id": dtl.findtext("BillId", ""),
        "control_number": dtl.findtext("BillCntrNum", ""),
        "status_code": dtl.findtext("BillStsCode", ""),
        "status_desc": dtl.findtext("BillStsDesc", ""),
    }


def build_bill_response_ack(res_id: str, status_code: str = "7101") -> str:
    """Build a signed billSubResAck response for GePG."""
    ack = f"""<?xml version="1.0" encoding="UTF-8"?>
<Gepg>
    <billSubResAck>
        <AckId>{generate_req_id()}</AckId>
        <ResId>{escape_xml(res_id)}</ResId>
        <AckStsCode>{status_code}</AckStsCode>
    </billSubResAck>
    <signature>SignatureGoesHere</signature>
</Gepg>"""
    return sign_if_enabled(ack)


def build_reconciliation_ack(res_id: str, status_code: str = "7101") -> str:
    """Build the acknowledgement for a ``sucSpPmtRes`` callback."""
    ack = f"""<?xml version="1.0" encoding="UTF-8"?>
<Gepg><sucSpPmtResAck><AckId>{generate_req_id()}</AckId>
<ResId>{escape_xml(res_id)}</ResId><AckStsCode>{status_code}</AckStsCode>
</sucSpPmtResAck><signature>SignatureGoesHere</signature></Gepg>"""
    return sign_if_enabled(ack)
