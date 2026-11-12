"""Serializers for the billing domain."""

from decimal import Decimal

from rest_framework import serializers

from apps.core.serializers import AuditFieldsMixin, DisplayReferenceField
from apps.core.services import format_reference_number
from apps.orders.models import Order
from apps.orders.serializers import StoneSerializer

from .models import (
    Bill,
    BillItem,
    Payment,
    Reconciliation,
    ReconciliationTransaction,
    ServiceProvider,
)


class ServiceProviderSerializer(AuditFieldsMixin):
    """A GePG service provider."""

    class Meta:
        model = ServiceProvider
        fields = (
            "id",
            "sp_code",
            "name",
            "group_code",
            "sys_code",
            "is_active",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        read_only_fields = AuditFieldsMixin.AUDIT_FIELDS


class BillItemSerializer(AuditFieldsMixin):
    """One charge line on a bill."""

    stone_label = serializers.CharField(source="stone.label", read_only=True)

    class Meta:
        model = BillItem
        fields = (
            "id",
            "bill",
            "stone",
            "stone_label",
            "description",
            "unit_price",
            "weight",
            "amount",
            "gfs_code",
            "item_ref",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        # Items are snapshots written by the billing service, never edited.
        read_only_fields = fields


class PaymentSerializer(AuditFieldsMixin):
    """A payment notification as the gateway sent it."""

    class Meta:
        model = Payment
        fields = (
            "id",
            "bill",
            "req_id",
            "grp_bill_id",
            "sp_grp_code",
            "cust_cntr_num",
            "entry_count",
            "sp_code",
            "gepg_bill_id",
            "bill_ctr_num",
            "psp_code",
            "psp_name",
            "trx_id",
            "pay_ref_id",
            "bill_amount",
            "paid_amount",
            "bill_pay_opt",
            "currency",
            "coll_acc_num",
            "trx_dt_tm",
            "usd_pay_chnl",
            "pyr_cell_num",
            "pyr_email",
            "pyr_name",
            "ack_id",
            "ack_sts_code",
            "is_processed",
            "raw_request",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        read_only_fields = fields


class BillSerializer(AuditFieldsMixin):
    """A bill, with its line items and the gateway's last word on it."""

    bill_number = DisplayReferenceField(read_only=True)
    order_reference = DisplayReferenceField(
        source="order.reference_number", read_only=True
    )
    customer_name = serializers.CharField(
        source="order.customer.full_name", read_only=True
    )
    service_provider_detail = ServiceProviderSerializer(
        source="service_provider", read_only=True
    )
    items = BillItemSerializer(many=True, read_only=True)
    amount_paid = serializers.SerializerMethodField()

    class Meta:
        model = Bill
        fields = (
            "id",
            "order",
            "order_reference",
            "customer_name",
            "bill_number",
            "control_number",
            "service_provider",
            "service_provider_detail",
            "items",
            "total_amount",
            "amount_paid",
            "currency",
            "status",
            "issued_at",
            "expiry_at",
            "due_date",
            "bill_type",
            "pay_type",
            "status_code",
            "status_desc",
            "is_gepg_submitted",
            "gepg_submitted_at",
            "control_number_sms_sent_at",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        # A bill is created by the billing service and thereafter written only by
        # the gateway callbacks, so nothing here is client-writable.
        read_only_fields = fields

    def get_amount_paid(self, obj) -> str:
        """Sum of the payments received, as a string to preserve the decimal."""
        total = sum((payment.paid_amount or 0) for payment in obj.payments.all())
        return str(total)


class GenerateBillSerializer(serializers.Serializer):
    """Payload for generating an order's bill."""

    service_provider = serializers.PrimaryKeyRelatedField(
        queryset=ServiceProvider.objects.all(), required=False, allow_null=True
    )


class RetryBillSerializer(serializers.Serializer):
    """Identify the order whose failed automatic billing should be retried."""

    order = serializers.PrimaryKeyRelatedField(queryset=Order.objects.all())


class IdentifiedStoneSerializer(StoneSerializer):
    """Stone response with the automatic billing result for the final pick."""

    billing_attention = serializers.SerializerMethodField()
    bill_number = serializers.SerializerMethodField()
    control_number = serializers.SerializerMethodField()

    class Meta(StoneSerializer.Meta):
        fields = (
            *StoneSerializer.Meta.fields,
            "billing_attention",
            "bill_number",
            "control_number",
        )

    def get_billing_attention(self, obj) -> str | None:
        """Explain why a saved identification needs a billing retry."""
        return self.context.get("billing_attention")

    def get_bill_number(self, obj) -> str | None:
        """Return the bill created by this identification, if any."""
        bill = self.context.get("bill")
        return format_reference_number(bill.bill_number) if bill else None

    def get_control_number(self, obj) -> str | None:
        """Return a synchronous GePG number; an accepted callback may follow."""
        bill = self.context.get("bill")
        return bill.control_number or None if bill else None


class BillPreviewItemSerializer(serializers.Serializer):
    """One line the bill *would* carry, priced but not written."""

    stone = serializers.IntegerField()
    label = serializers.CharField()
    description = serializers.CharField()
    category = serializers.CharField()
    # Null when the stone's category carries no fee - a configuration gap the
    # screen names rather than hides.
    amount = serializers.DecimalField(max_digits=15, decimal_places=2, allow_null=True)


class BillPreviewSerializer(serializers.Serializer):
    """What generating a bill for an order would produce.

    Read-only and side-effect free: this is the figures shown to whoever is
    about to commit to them.
    """

    items = BillPreviewItemSerializer(many=True)
    total = serializers.DecimalField(max_digits=15, decimal_places=2)
    currency = serializers.CharField()
    #: Reasons the order cannot be billed. Empty means it can.
    blockers = serializers.ListField(child=serializers.CharField())


class BillCancellationSerializer(serializers.Serializer):
    """Reason required to submit a cancellation request."""

    reason = serializers.CharField(max_length=500)


class ReconciliationRequestSerializer(serializers.Serializer):
    """Date of payments to reconcile; defaults to the current local date."""

    trx_date = serializers.DateField(required=False)


class ReconciliationTransactionSerializer(serializers.ModelSerializer):
    """Read-only transaction detail returned in a reconciliation batch."""

    class Meta:
        model = ReconciliationTransaction
        fields = "__all__"


class ReconciliationSerializer(serializers.ModelSerializer):
    """Reconciliation request, status and returned transaction rows."""

    transactions = ReconciliationTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = Reconciliation
        fields = "__all__"
