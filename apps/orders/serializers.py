"""Serializers for the order domain."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.serializers import AuditFieldsMixin, DisplayReferenceField
from apps.core.services import format_reference_number, reference_number_for_order
from apps.gems.enums import OrderHold, OrderStage, StoneStatus
from apps.gems.models import StoneCategory, StoneType
from apps.gems.serializers import StoneCategorySerializer, StoneTypeSerializer

from .models import Customer, Order, StatusHistory, Stone
from .selectors import order_stage
from .services import next_stone_label, pending_order_copy_available_at


class CustomerSerializer(AuditFieldsMixin):
    """A submitting customer."""

    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = Customer
        fields = (
            "id",
            "first_name",
            "middle_name",
            "last_name",
            "full_name",
            "phone",
            "email",
            "company_name",
            "region",
            "id_number",
            "address",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        read_only_fields = AuditFieldsMixin.AUDIT_FIELDS


class StoneReportSerializer(serializers.Serializer):
    """The slice of an identification report a stone's row needs.

    A plain ``Serializer``, not a ModelSerializer, so ``orders`` does not import
    the ``identification`` models - the two apps sit at the same layer. It is
    read-only and exists only to give ``report_detail`` a documented shape.
    """

    id = serializers.IntegerField(read_only=True)
    report_number = DisplayReferenceField(read_only=True)
    is_finalized = serializers.BooleanField(read_only=True)


class StoneSerializer(AuditFieldsMixin):
    """One stone in an order.

    ``status`` is read-only: it moves through ``POST {id}/transition/`` so that
    every change writes a history row. A writable field here would let a client
    change the status without leaving a trace.
    """

    stone_category = serializers.PrimaryKeyRelatedField(
        queryset=StoneCategory.objects.all(), required=False
    )
    stone_type = serializers.PrimaryKeyRelatedField(
        queryset=StoneType.objects.all(), required=False, allow_null=True
    )
    stone_category_detail = StoneCategorySerializer(
        source="stone_category", read_only=True
    )
    stone_type_detail = StoneTypeSerializer(source="stone_type", read_only=True)
    order_reference = DisplayReferenceField(
        source="order.reference_number", read_only=True
    )
    stone_reference = serializers.SerializerMethodField()
    # The customer, alongside the order the stone came in on. A reference number
    # identifies the paperwork; the name identifies the visit.
    customer_name = serializers.CharField(
        source="order.customer.full_name", read_only=True
    )
    customer_phone = serializers.CharField(source="order.customer.phone", read_only=True)
    report_detail = serializers.SerializerMethodField()

    class Meta:
        model = Stone
        fields = (
            "id",
            "order",
            "order_reference",
            "stone_reference",
            "customer_name",
            "customer_phone",
            "label",
            "stone_category",
            "stone_category_detail",
            "stone_type",
            "stone_type_detail",
            "weight",
            "weight_unit",
            "photo",
            "status",
            "report_detail",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        read_only_fields = (*AuditFieldsMixin.AUDIT_FIELDS, "label", "status", "order")

    def validate(self, attrs):
        """Keep categories and any selected legacy type consistent."""
        category = attrs.get("stone_category")
        stone_type = attrs.get("stone_type")

        if stone_type is not None and category is None:
            category = stone_type.category
            attrs["stone_category"] = category

        if category is None and self.instance is None:
            raise serializers.ValidationError(
                {"stone_category": "Select a stone category."}
            )

        if (
            category is not None
            and stone_type is not None
            and stone_type.category_id != category.pk
        ):
            raise serializers.ValidationError(
                {"stone_type": "Choose a type belonging to this category."}
            )
        return attrs

    @extend_schema_field(StoneReportSerializer)
    def get_report_detail(self, stone):
        """The stone's identification report, or ``None`` if it has none yet.

        Just enough for a caller to tell the three states of a stone apart - no
        report, a draft, a finalized one - without a second request. The findings
        queue turns on exactly this: a stone with a draft must be edited, not
        recorded again, because ``Stone.report`` is a OneToOne and a second
        create is rejected as a duplicate.

        Declared here rather than as a nested serializer so ``orders`` keeps its
        one-way relationship with ``identification``: the reverse accessor is
        reached by attribute, never by import.
        """
        report = getattr(stone, "report", None)
        return StoneReportSerializer(report).data if report else None

    def get_stone_reference(self, stone) -> str:
        """Identify this stone under its order's shared sequence."""
        return format_reference_number(
            reference_number_for_order(stone.order, "ORD", stone_label=stone.label)
        )


class OrderSerializer(AuditFieldsMixin):
    """An order, with its customer and progress counters.

    The project's one nested write. Every other nested serializer here is
    read-only, and this is the exception because reception meets most customers
    for the first time while receiving their order: send ``customer`` for
    somebody on file, or ``customer_data`` to register them alongside the order.
    """

    customer = serializers.PrimaryKeyRelatedField(
        queryset=Customer.objects.all(), required=False
    )
    customer_detail = CustomerSerializer(source="customer", read_only=True)
    # The bill raised against this order, if any.
    #
    # Exposed so a screen can tell "ready to bill" from "already billed"
    # without a second request. `apps.orders` sits below `apps.billing`, so
    # this reads the reverse relation rather than importing it.
    reference_number = DisplayReferenceField(read_only=True)
    bill_number = serializers.SerializerMethodField()
    control_number = serializers.SerializerMethodField()
    bill_total_amount = serializers.SerializerMethodField()
    bill_currency = serializers.SerializerMethodField()
    copy_available_at = serializers.SerializerMethodField()
    # The label the next stone identified here will carry, so the identification
    # dialog can name it before it exists.
    next_stone_label = serializers.SerializerMethodField()
    # Where the order has got to, derived from its stones and its bill.
    stage = serializers.SerializerMethodField()
    stage_label = serializers.SerializerMethodField()
    held_by_label = serializers.SerializerMethodField()
    customer_data = CustomerSerializer(required=False, write_only=True)
    identified_count = serializers.IntegerField(read_only=True)

    def validate(self, attrs):
        """Require exactly one of ``customer`` and ``customer_data``.

        Both together is an error rather than a precedence rule: it means the
        client is confused about which customer this order belongs to, and
        silently picking one would attach the order to the wrong person.
        """
        # On PATCH, an untouched customer is not a missing one.
        if self.instance is not None and "customer_data" not in attrs:
            return attrs

        has_ref = attrs.get("customer") is not None
        has_data = bool(attrs.get("customer_data"))

        if has_ref and has_data:
            raise serializers.ValidationError(
                "Send either an existing customer or new customer details, not both."
            )
        if not has_ref and not has_data and self.instance is None:
            raise serializers.ValidationError(
                {"customer": "Select a customer or enter their details."}
            )
        return attrs

    class Meta:
        model = Order
        fields = (
            "id",
            "reference_number",
            "customer",
            "customer_detail",
            "customer_data",
            "received_date",
            "stone_count",
            "identified_count",
            "next_stone_label",
            "next_stone_label",
            "bill_number",
            "control_number",
            "bill_total_amount",
            "bill_currency",
            "copy_available_at",
            "stage",
            "stage_label",
            "hold_status",
            "hold_reason",
            "held_by",
            "held_by_label",
            "held_at",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        # The reference number is allocated by the service, never supplied.
        read_only_fields = (
            *AuditFieldsMixin.AUDIT_FIELDS,
            "reference_number",
            "bill_number",
            "control_number",
            "bill_total_amount",
            "bill_currency",
            "stage",
            "stage_label",
            "hold_status",
            "hold_reason",
            "held_by",
            "held_by_label",
            "held_at",
        )

    def get_bill_number(self, obj) -> str | None:
        """This order's bill number, or None if it has not been billed."""
        bill = getattr(obj, "bill", None)
        return format_reference_number(bill.bill_number) if bill is not None else None

    def get_next_stone_label(self, obj) -> str | None:
        """What the next stone will be called, or None when the order is full."""
        if obj.identified_count >= obj.stone_count:
            return None
        return next_stone_label(obj)

    def get_control_number(self, obj) -> str | None:
        """The number the customer quotes when paying, once GePG has issued one.

        Allocated by the gateway in response to the bill submission, not minted
        here - so it stays null on a bill whose submission failed, which is
        exactly the case worth seeing from the orders list.
        """
        bill = getattr(obj, "bill", None)
        if bill is None or bill.control_number in ("", "PENDING"):
            return None
        return bill.control_number

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_copy_available_at(self, obj):
        """When an unresolved submitted bill can be copied, if ever."""
        return pending_order_copy_available_at(obj)

    def get_bill_total_amount(self, obj) -> str | None:
        """The amount charged on this order's bill, or None if it is unbilled."""
        bill = getattr(obj, "bill", None)
        return str(bill.total_amount) if bill is not None else None

    def get_bill_currency(self, obj) -> str | None:
        """The currency of this order's bill, or None if it is unbilled."""
        bill = getattr(obj, "bill", None)
        return bill.currency if bill is not None else None

    def get_stage(self, obj) -> str:
        """The stage code, for a client to style and filter on."""
        return order_stage(obj)

    def get_stage_label(self, obj) -> str:
        """The stage in words, so a client need not mirror the enum."""
        return OrderStage(order_stage(obj)).label

    def get_held_by_label(self, obj) -> str | None:
        """Who paused or withdrew the order, or None if it is active."""
        return str(obj.held_by) if obj.held_by_id else None


class StatusHistorySerializer(serializers.ModelSerializer):
    """One entry in a stone's status trail.

    Plain ``ModelSerializer`` rather than ``AuditFieldsMixin``: the model is an
    append-only ledger with no audit columns of its own.
    """

    changed_by_label = serializers.SerializerMethodField()

    class Meta:
        model = StatusHistory
        fields = (
            "id",
            "stone",
            "from_status",
            "to_status",
            "changed_by",
            "changed_by_label",
            "changed_at",
            "note",
        )
        read_only_fields = fields

    def get_changed_by_label(self, obj) -> str:
        """Who made the change, or 'System' when the gateway did."""
        return str(obj.changed_by) if obj.changed_by_id else "System"


class AddStoneSerializer(serializers.Serializer):
    """Payload for identifying one stone.

    The category prices the bill. ``stone_type`` remains accepted for older
    clients and derives its category; exact type is recorded later at the bench.
    """

    stone_category = serializers.PrimaryKeyRelatedField(
        queryset=StoneCategory.objects.all(), required=False
    )
    stone_type = serializers.PrimaryKeyRelatedField(
        queryset=StoneType.objects.all(), required=False
    )

    def validate(self, attrs):
        """Derive a legacy category and reject type/category mismatches."""
        category = attrs.get("stone_category")
        stone_type = attrs.get("stone_type")
        if category is None and stone_type is not None:
            category = stone_type.category
            attrs["stone_category"] = category
        if category is None:
            raise serializers.ValidationError(
                {"stone_category": "Select a stone category."}
            )
        if stone_type is not None and stone_type.category_id != category.pk:
            raise serializers.ValidationError(
                {"stone_type": "Choose a type belonging to this category."}
            )
        return attrs


class TransitionSerializer(serializers.Serializer):
    """Payload for moving a stone to a new status."""

    to_status = serializers.ChoiceField(choices=StoneStatus.choices)
    note = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


class HoldOrderSerializer(serializers.Serializer):
    """Payload for pausing or withdrawing an order.

    ``reason`` is required rather than optional: a held order that says nothing
    about why is a question for whoever finds it next, and the person who knows
    the answer is the one filling this in.
    """

    hold_status = serializers.ChoiceField(
        choices=[OrderHold.ON_HOLD, OrderHold.CANCELLED]
    )
    reason = serializers.CharField(max_length=500)
