"""API views for the order domain."""

from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from django.conf import settings
from django.db import transaction
from django.db.models import F

from apps.core.permissions import OrdersOrStonesViewPermission, StrictModelPermissions
from apps.core.viewsets import BaseModelViewSet
from apps.core.workflow_feed import feed_row, paginated_workflow_feed

from .models import Customer, Order, StatusHistory, Stone
from .search import ORDER_SEARCH_FIELDS, STONE_SEARCH_FIELDS
from .selectors import (
    annotate_identified,
    identification_action_queryset,
    identification_worklist,
    orders_at_stage,
)
from .serializers import (
    CustomerSerializer,
    HoldOrderSerializer,
    OrderSerializer,
    StatusHistorySerializer,
    StoneSerializer,
    TransitionSerializer,
)
from .services import (
    assert_stone_retypeable,
    copy_pending_order,
    create_order,
    hold_order,
    release_order,
    transition_stone,
    update_stone,
)


class CustomerViewSet(BaseModelViewSet, viewsets.ModelViewSet):
    """CRUD over customers."""

    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    search_fields = (
        "first_name",
        "middle_name",
        "last_name",
        "phone",
        "email",
        "company_name",
        "id_number",
    )
    filter_fields = ("region", "company_name")
    ordering_fields = ("id", "first_name", "last_name", "phone", "created_at")


class OrderViewSet(BaseModelViewSet, viewsets.ModelViewSet):
    """CRUD over orders, plus identification of their stones.

    Accepts one query param beyond the shared list contract:
    ``?identification=pending`` for orders with stones still to type, or
    ``complete`` for those fully typed. Anything else is ignored.

    Deliberately **not** a ``filter[...]`` key: that vocabulary is for field
    lookups ``WhitelistFilterBackend`` validates against a whitelist, and this
    is a comparison between two columns (``Count(stones)`` against
    ``stone_count``) that no field lookup can express. Smuggling it through
    ``filter[...]`` would make the whitelist a liar about what it checks.
    """

    # Every row serialises its customer, and identified_count counts the stones.
    queryset = Order.objects.select_related("customer", "bill").prefetch_related("stones")
    serializer_class = OrderSerializer

    search_fields = ORDER_SEARCH_FIELDS
    filter_fields = ("customer", "received_date")
    ordering_fields = ("id", "reference_number", "received_date", "created_at")
    date_filter_fields = ("created_at", "updated_at", "received_date")

    # Both entries name the same permission on purpose: the preliminary queue
    # exists to be worked, not browsed, so whoever may identify a stone is
    # exactly who should see the queue of stones waiting to be identified. This
    # follows the billing and certification worklists, which gate on their
    # workflow verb rather than on `view`.
    def get_queryset(self):
        """Narrow by identification progress and/or the order's derived stage.

        Both live here rather than in ``filter_fields`` for the same reason: the
        whitelist filter backend matches fields, and neither of these is one.
        ``identification`` compares two columns; ``stage`` is computed from the
        stones and the bill by :func:`order_stage`, and
        :func:`orders_at_stage` re-expresses that derivation as SQL.

        The two combine rather than one overriding the other. A hold or a
        cancellation outranks identification in the derivation, so
        ``stage=on_hold`` alone would still return orders with stones left to
        type - wrong for a screen that lists only finished identification.
        Re-annotating ``identified`` after ``orders_at_stage`` has already done
        so replaces the alias on the same join, so the count is not inflated.
        """
        queryset = super().get_queryset()
        params = getattr(self.request, "query_params", {})

        stage = str(params.get("stage", "")).lower()
        if stage:
            queryset = orders_at_stage(queryset, stage)

        wanted = str(params.get("identification", "")).lower()
        if wanted == "pending":
            return annotate_identified(queryset).filter(identified__lt=F("stone_count"))
        if wanted == "complete":
            return annotate_identified(queryset).filter(identified__gte=F("stone_count"))
        return queryset

    action_permissions = {
        "worklist": ["orders.add_stone"],
        "hold": ["orders.hold_order"],
        "release": ["orders.hold_order"],
        "copy_after_gepg_timeout": ["orders.add_order"],
    }

    def perform_create(self, serializer):
        """Delegate to the service, which allocates the reference number.

        A nested ``customer_data`` registers the customer in the same
        transaction. The permission check below is load-bearing:
        ``StrictModelPermissions`` only asks for ``orders.add_order`` on this
        endpoint, so without it the nested write would be a way to create a
        customer without holding ``orders.add_customer``.
        """
        customer_data = serializer.validated_data.get("customer_data")
        if customer_data and not self.request.user.has_perm("orders.add_customer"):
            raise PermissionDenied("You may not register new customers.")

        order = create_order(
            customer=serializer.validated_data.get("customer"),
            customer_data=customer_data,
            stone_count=serializer.validated_data.get("stone_count", 0),
            received_date=serializer.validated_data.get("received_date"),
            user=self.request.user,
        )
        serializer.instance = order

    def perform_update(self, serializer):
        """Lock order details after GePG has returned its control number."""
        bill = getattr(serializer.instance, "bill", None)
        if bill is not None and bill.control_number:
            raise PermissionDenied(
                "This order cannot be edited after GePG returns its control number."
            )
        serializer.save()

    @extend_schema(responses=OrderSerializer)
    @extend_schema(request=HoldOrderSerializer, responses=OrderSerializer)
    @action(detail=True, methods=["post"])
    def hold(self, request, pk=None):
        """Pause or withdraw the whole order.

        The one piece of an order's state that is written rather than derived:
        a customer asking the lab to stop is a fact about the visit, not about
        any stone in it.
        """
        payload = HoldOrderSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        order = hold_order(
            self.get_object(),
            status=payload.validated_data["hold_status"],
            reason=payload.validated_data["reason"],
            user=request.user,
        )
        return Response(self.get_serializer(order).data)

    @extend_schema(request=None, responses=OrderSerializer)
    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        """Return a held or cancelled order to active work."""
        order = release_order(self.get_object(), user=request.user)
        return Response(self.get_serializer(order).data)

    @extend_schema(request=None, responses={201: OrderSerializer})
    @action(
        detail=True,
        methods=["post"],
        url_path="copy-after-gepg-timeout",
    )
    def copy_after_gepg_timeout(self, request, pk=None):
        """Create an independent copy after a GePG bill remains unresolved."""
        order = copy_pending_order(self.get_object(), user=request.user)
        return Response(
            self.get_serializer(order).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"])
    def worklist(self, request):
        """Orders with stones still to identify - the bench's intake queue."""
        queryset = self.filter_queryset(identification_worklist())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    @action(detail=False, methods=["get"], url_path="workflow-feed")
    def workflow_feed(self, request):
        """Orders with identification actions still available."""
        queryset = identification_action_queryset().order_by("-received_date", "-pk")
        payload = OrderSerializer(
            queryset, many=True, context=self.get_serializer_context()
        ).data
        waiting_ids = set(
            queryset.filter(identified__lt=F("stone_count")).values_list("pk", flat=True)
        )
        rows = [
            feed_row(
                kind="order",
                record_id=row["id"],
                reference=row["reference_number"],
                customer=(row.get("customer_detail") or {}).get("full_name"),
                type_name="Order",
                status=row.get("stage_label") or row.get("stage"),
                date=row.get("received_date"),
                waiting=row["id"] in waiting_ids,
                detail=row,
            )
            for row in payload
        ]
        return paginated_workflow_feed(self, rows, request)

    @action(
        detail=False,
        methods=["get"],
        url_path="identification-count",
        permission_classes=[OrdersOrStonesViewPermission],
    )
    def identification_count(self, request):
        """Pending identification count for every viewer of the main entry."""
        count = (
            identification_action_queryset()
            .filter(identified__lt=F("stone_count"))
            .count()
        )
        return Response({"count": count})


class StoneViewSet(BaseModelViewSet, viewsets.ModelViewSet):
    """CRUD over stones, plus status transitions."""

    # `report_detail` is serialised on every stone row - the queues read it to
    # tell a stone waiting for findings from one whose draft is already open.
    # Prefetched, not joined: the relation is a reverse FK so that a discarded
    # report frees the stone rather than occupying it forever.
    queryset = Stone.objects.select_related(
        "order",
        "order__customer",
        "stone_category",
        "stone_type",
        "stone_type__category",
    ).prefetch_related("reports")
    serializer_class = StoneSerializer

    search_fields = STONE_SEARCH_FIELDS
    filter_fields = ("order", "stone_category", "stone_type", "status", "weight_unit")
    ordering_fields = ("id", "label", "status", "weight", "created_at")

    action_permissions = {"transition": ["orders.transition_stone"]}

    @transaction.atomic
    def perform_update(self, serializer):
        """Delegate to the service, so every stone write goes through one door.

        ``StoneSerializer``'s writable fields are exactly the service's
        keywords; adding a writable field there means adding it here too.
        """
        serializer.instance = update_stone(
            serializer.instance, user=self.request.user, **serializer.validated_data
        )
        report = serializer.instance.report
        if report is not None and report.is_finalized:
            from apps.certificates.services import refresh_certificate_snapshot

            refresh_certificate_snapshot(report, user=self.request.user)

    def perform_destroy(self, instance):
        """Refuse to delete a stone a bill was priced from.

        Deletion goes through ``SoftDeleteViewSetMixin`` rather than the
        service, so the guard is repeated here - there is no single door for
        this one. Reuses the retypeable check because it asks the same question:
        has a bill been raised against this stone yet.
        """
        if settings.AUTO_BILL_AFTER_IDENTIFICATION:
            raise PermissionDenied(
                "Preliminary identification cannot be removed while automatic "
                "billing is enabled."
            )
        assert_stone_retypeable(instance)
        super().perform_destroy(instance)

    @extend_schema(request=TransitionSerializer, responses=StoneSerializer)
    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Move this stone to a new status, writing a history entry."""
        stone = self.get_object()
        payload = TransitionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        stone = transition_stone(
            stone,
            payload.validated_data["to_status"],
            user=request.user,
            note=payload.validated_data.get("note", ""),
        )
        return Response(self.get_serializer(stone).data)


class StatusHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only view of stone status transitions.

    Not a ``BaseModelViewSet``: the model is an append-only ledger, so there is
    nothing to soft-delete and nothing to restore.
    """

    queryset = StatusHistory.objects.select_related("stone", "changed_by")
    serializer_class = StatusHistorySerializer
    permission_classes = [StrictModelPermissions]

    filter_fields = ("stone", "to_status", "from_status", "changed_by")
    ordering_fields = ("id", "changed_at")
    ordering = ["-changed_at"]
    date_filter_fields = ("changed_at",)
