"""Bill (one per order), its line items, and payments."""

from django.db import models
from django.db.models import Q

from apps.core.models import BaseModel
from apps.gems.enums import BillStatus

from ..constants import CURRENCY_TZS
from .provider import ServiceProvider


class Bill(BaseModel):
    """A single bill for an order. Total is the sum of its items.

    ``status`` is the domain state; the ``status_code``/``status_desc`` pair
    below is whatever the gateway last said, kept raw so a failed submission can
    be diagnosed from the row itself.
    """

    order = models.OneToOneField(
        "orders.Order", on_delete=models.PROTECT, related_name="bill"
    )
    bill_number = models.CharField(max_length=50)
    control_number = models.CharField(max_length=50, blank=True, default="")
    service_provider = models.ForeignKey(
        ServiceProvider,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bills",
    )
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    currency = models.CharField(
        max_length=3, choices=[(CURRENCY_TZS, CURRENCY_TZS)], default=CURRENCY_TZS
    )
    status = models.CharField(
        max_length=20, choices=BillStatus.choices, default=BillStatus.PENDING
    )
    issued_at = models.DateTimeField(null=True, blank=True)
    expiry_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    # GePG submission tracking (raw gateway state).
    bill_type = models.PositiveSmallIntegerField(default=1)
    pay_type = models.PositiveSmallIntegerField(default=1)
    status_code = models.CharField(max_length=30, blank=True, default="")
    status_desc = models.CharField(max_length=255, blank=True, default="")
    is_gepg_submitted = models.BooleanField(default=False)
    gepg_submitted_at = models.DateTimeField(null=True, blank=True)
    control_number_sms_sent_at = models.DateTimeField(null=True, blank=True)
    control_number_sms_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("generate_bill", "Can generate a bill")]
        constraints = [
            models.CheckConstraint(
                condition=Q(currency=CURRENCY_TZS), name="billing_bill_currency_tzs"
            ),
            models.UniqueConstraint(
                fields=["bill_number"],
                condition=Q(deleted_at__isnull=True),
                name="%(app_label)s_%(class)s_unique_bill_number",
            ),
            # The blank exclusion matters: a control number arrives later (or
            # never, if the gateway was unreachable), so many bills legitimately
            # sit at "" and must not collide with one another.
            models.UniqueConstraint(
                fields=["control_number"],
                condition=Q(deleted_at__isnull=True) & ~Q(control_number=""),
                name="%(app_label)s_%(class)s_unique_control_number",
            ),
        ]

    def __str__(self) -> str:
        return self.bill_number


class BillItem(BaseModel):
    """A charge line for one stone.

    The price fields are snapshots taken when the bill was issued, so repricing
    a stone type later never rewrites what a customer was actually charged.
    """

    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="items")
    stone = models.ForeignKey(
        "orders.Stone", on_delete=models.PROTECT, related_name="bill_items"
    )
    description = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)
    # Null at billing time: weight is recorded during findings, which happen
    # after payment.
    weight = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    gfs_code = models.CharField(max_length=20, blank=True, default="")
    item_ref = models.CharField(max_length=50, blank=True, default="")

    def __str__(self) -> str:
        return self.description


class Payment(BaseModel):
    """A payment notification received from GePG (one per transaction).

    ``raw_request`` preserves the gateway's original message for disputes.
    Only TZS notifications are accepted into the structured payment fields.
    """

    bill = models.ForeignKey(
        Bill, on_delete=models.PROTECT, related_name="payments", null=True, blank=True
    )

    # Payment header (PmtHdr)
    req_id = models.CharField(max_length=100, blank=True, default="")
    grp_bill_id = models.CharField(max_length=100, blank=True, default="")
    sp_grp_code = models.CharField(max_length=10, blank=True, default="")
    cust_cntr_num = models.CharField(max_length=12, blank=True, default="")
    entry_count = models.PositiveIntegerField(null=True, blank=True)

    # Transaction details (PmtTrxDtl)
    sp_code = models.CharField(max_length=10, blank=True, default="")
    gepg_bill_id = models.CharField(max_length=100, blank=True, default="")
    bill_ctr_num = models.CharField(max_length=12, blank=True, default="")
    psp_code = models.CharField(max_length=10, blank=True, default="")
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
    trx_dt_tm = models.DateTimeField(null=True, blank=True)
    usd_pay_chnl = models.CharField(max_length=50, blank=True, default="")
    pyr_cell_num = models.CharField(max_length=15, blank=True, default="")
    pyr_email = models.CharField(max_length=150, blank=True, default="")
    pyr_name = models.CharField(max_length=200, blank=True, default="")

    # Acknowledgement we returned + raw payload
    ack_id = models.CharField(max_length=100, blank=True, default="")
    ack_sts_code = models.CharField(max_length=10, blank=True, default="")
    is_processed = models.BooleanField(default=False)
    raw_request = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-trx_dt_tm"]
        constraints = [
            models.CheckConstraint(
                condition=Q(currency=CURRENCY_TZS), name="billing_payment_currency_tzs"
            ),
            # The idempotency key for a redelivered notification.
            models.UniqueConstraint(
                fields=["trx_id"],
                condition=Q(deleted_at__isnull=True) & ~Q(trx_id=""),
                name="%(app_label)s_%(class)s_unique_trx_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.paid_amount or 0} on {self.gepg_bill_id} ({self.trx_id})"
