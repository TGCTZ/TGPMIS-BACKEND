"""Auditable records for GePG cancellation and reconciliation workflows."""

from django.db import models
from django.db.models import Q

from apps.core.models import BaseModel

from ..constants import CURRENCY_TZS


class BillCancellation(BaseModel):
    """One cancellation request and its gateway acknowledgements."""

    bill = models.ForeignKey(
        "billing.Bill", on_delete=models.PROTECT, related_name="cancellations"
    )
    req_id = models.CharField(max_length=100, unique=True)
    reason = models.CharField(max_length=500)
    status_code = models.CharField(max_length=30, blank=True, default="")
    status_desc = models.CharField(max_length=255, blank=True, default="")
    request_xml = models.TextField(blank=True, default="")
    response_xml = models.TextField(blank=True, default="")


class Reconciliation(BaseModel):
    """Daily request lifecycle and raw GePG messages."""

    req_id = models.CharField(max_length=100, unique=True)
    trx_dt = models.DateField()
    status = models.CharField(max_length=20, default="pending")
    ack_id = models.CharField(max_length=100, blank=True, default="")
    ack_status_code = models.CharField(max_length=20, blank=True, default="")
    ack_status_desc = models.CharField(max_length=255, blank=True, default="")
    res_id = models.CharField(max_length=100, blank=True, default="")
    payment_status_code = models.CharField(max_length=20, blank=True, default="")
    payment_status_desc = models.CharField(max_length=255, blank=True, default="")
    request_xml = models.TextField(blank=True, default="")
    acknowledgement_xml = models.TextField(blank=True, default="")
    response_xml = models.TextField(blank=True, default="")
    response_ack_xml = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-trx_dt", "-created_at"]


class ReconciliationTransaction(BaseModel):
    """A payment row returned in a reconciliation batch."""

    reconciliation = models.ForeignKey(
        Reconciliation, on_delete=models.CASCADE, related_name="transactions"
    )
    cust_cntr_num = models.CharField(max_length=30, blank=True, default="")
    grp_bill_id = models.CharField(max_length=100, blank=True, default="")
    sp_code = models.CharField(max_length=20, blank=True, default="")
    bill_id = models.CharField(max_length=100, blank=True, default="")
    bill_ctr_num = models.CharField(max_length=30, blank=True, default="")
    psp_code = models.CharField(max_length=30, blank=True, default="")
    psp_name = models.CharField(max_length=200, blank=True, default="")
    trx_id = models.CharField(max_length=100, blank=True, default="")
    pay_ref_id = models.CharField(max_length=100, blank=True, default="")
    bill_amount = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    paid_amount = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    bill_pay_opt = models.CharField(max_length=1, blank=True, default="")
    currency = models.CharField(
        max_length=3, choices=[(CURRENCY_TZS, CURRENCY_TZS)], default=CURRENCY_TZS
    )
    coll_acc_num = models.CharField(max_length=50, blank=True, default="")
    trx_dt_tm = models.CharField(max_length=40, blank=True, default="")
    usd_pay_chnl = models.CharField(max_length=50, blank=True, default="")
    trd_pty_trx_id = models.CharField(max_length=100, blank=True, default="")
    qt_ref_id = models.CharField(max_length=100, blank=True, default="")
    pyr_cell_num = models.CharField(max_length=15, blank=True, default="")
    pyr_email = models.CharField(max_length=150, blank=True, default="")
    pyr_name = models.CharField(max_length=200, blank=True, default="")
    rsv1 = models.CharField(max_length=255, blank=True, default="")
    rsv2 = models.CharField(max_length=255, blank=True, default="")
    rsv3 = models.CharField(max_length=255, blank=True, default="")
    raw_xml = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(currency=CURRENCY_TZS),
                name="billing_reconciliation_currency_tzs",
            )
        ]
