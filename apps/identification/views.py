"""API views for the identification domain."""

from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from django.db import transaction

from apps.certificates.services import issue_certificate, refresh_certificate_snapshot
from apps.core.exceptions import ServiceError
from apps.core.filters import search_queryset
from apps.core.viewsets import BaseModelViewSet
from apps.core.workflow_feed import feed_row, paginated_workflow_feed
from apps.orders.search import STONE_SEARCH_FIELDS
from apps.orders.serializers import StoneSerializer

from .models import IdentificationReport, InstrumentUsed
from .selectors import (
    findings_worklist,
)
from .selectors import (
    gemmologist_candidates as gemmologist_candidates_queryset,
)
from .serializers import (
    FinalizeReportSerializer,
    GemmologistCandidateSerializer,
    IdentificationReportSerializer,
    InstrumentUsedSerializer,
)
from .services import create_report, finalize_report, update_report


class IdentificationReportViewSet(BaseModelViewSet, viewsets.ModelViewSet):
    """CRUD over identification reports, plus the finalize action."""

    queryset = IdentificationReport.objects.select_related(
        "stone",
        "stone__stone_category",
        "stone__stone_type",
        "stone__stone_type__category",
        "stone__order",
        "stone__order__customer",
        "species",
        "variety",
        "origin",
        "shape_cut",
        "color",
        "treatment",
        "identified_by",
        "verified_by",
    ).prefetch_related("instruments_used", "instruments_used__instrument")
    serializer_class = IdentificationReportSerializer

    search_fields = (
        "report_number",
        "conclusion",
        "stone__label",
        "stone__order__reference_number",
    )
    filter_fields = (
        "stone",
        "is_finalized",
        "species",
        "variety",
        "origin",
        "shape_cut",
        "color",
        "treatment",
        "nature_type",
        "transparency",
        "optic_character",
        "is_polished",
    )
    ordering_fields = (
        "id",
        "report_number",
        "is_finalized",
        "identified_at",
        "created_at",
    )
    date_filter_fields = ("created_at", "updated_at", "identified_at")

    action_permissions = {
        "finalize": ["identification.finalize_report"],
        "worklist": ["identification.add_identificationreport"],
        # Gated on finalize rather than `users.view_user`: this list exists to
        # fill the finalize dialog, and the bench holds no permission on users.
        "gemmologist_candidates": ["identification.finalize_report"],
    }

    def check_restorable(self, instance):
        """Refuse to restore a report whose stone has been reported on again.

        Discarding a report frees its stone, so the bench may well have recorded
        fresh findings since. Only one live report per stone is allowed, and the
        newer one is the real record - so the older is left in the bin rather
        than the newer one silently displaced.
        """
        if (
            IdentificationReport.objects.filter(stone=instance.stone)
            .exclude(pk=instance.pk)
            .exists()
        ):
            raise ServiceError(
                f"Stone {instance.stone.label} already has a newer report. "
                "Delete that one first if this is the record you want back."
            )

    def perform_create(self, serializer):
        """Delegate to the service, which allocates the number and checks payment."""
        fields = dict(serializer.validated_data)
        stone = fields.pop("stone")
        serializer.instance = create_report(stone=stone, user=self.request.user, **fields)

    def perform_update(self, serializer):
        """Delegate to the service, which refuses a finalized report."""
        fields = dict(serializer.validated_data)
        # The stone a report belongs to is fixed at creation.
        fields.pop("stone", None)
        serializer.instance = update_report(
            serializer.instance, user=self.request.user, **fields
        )

    @extend_schema(
        request=FinalizeReportSerializer, responses=IdentificationReportSerializer
    )
    @action(detail=True, methods=["post"])
    @transaction.atomic
    def finalize(self, request, pk=None):
        """Finalize the report and issue its certificate as one transaction."""
        payload = FinalizeReportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        report = finalize_report(
            self.get_object(),
            user=request.user,
            verified_by=payload.validated_data.get("verified_by"),
        )
        issue_certificate(report.stone, user=request.user)
        return Response(self.get_serializer(report).data)

    @extend_schema(responses=GemmologistCandidateSerializer(many=True))
    @action(detail=False, methods=["get"], url_path="gemmologist-candidates")
    def gemmologist_candidates(self, request):
        """Active gemmologists the caller may name as second signatory.

        Unpaginated: the bench is a handful of people, and a dropdown that
        silently stopped at page one would hide colleagues rather than page.
        """
        candidates = gemmologist_candidates_queryset(exclude_user=request.user)
        return Response(GemmologistCandidateSerializer(candidates, many=True).data)

    @extend_schema(responses=StoneSerializer)
    @action(detail=False, methods=["get"])
    def worklist(self, request):
        """Paid stones whose findings are not finalized yet."""
        # Searched explicitly rather than via `filter_queryset`: this action
        # returns Stones, so the ViewSet's report `search_fields` do not apply.
        queryset = search_queryset(
            findings_worklist(), request.query_params.get("search"), STONE_SEARCH_FIELDS
        )
        page = self.paginate_queryset(queryset)
        serializer = StoneSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=False, methods=["get"], url_path="workflow-feed")
    def workflow_feed(self, request):
        """Open findings by default, with finalized reports available on request."""
        reports = self.get_queryset().order_by("-created_at", "-pk")
        if request.query_params.get("status") != "Finalized":
            reports = reports.filter(is_finalized=False)
        report_data = IdentificationReportSerializer(
            reports, many=True, context=self.get_serializer_context()
        ).data
        waiting_stones = list(findings_worklist())
        waiting_ids = {stone.pk for stone in waiting_stones}
        reports_by_stone = {row["stone"]: row for row in report_data}
        rows = []
        for row in report_data:
            draft = not row["is_finalized"]
            rows.append(
                feed_row(
                    kind="report",
                    record_id=row["id"],
                    reference=row.get("report_number") or row.get("order_reference"),
                    customer=row.get("customer_name"),
                    type_name="Findings",
                    status="Draft" if draft else "Finalized",
                    date=row.get("identified_at") or row.get("created_at"),
                    waiting=draft and row["stone"] in waiting_ids,
                    detail=row,
                )
            )
        for stone in waiting_stones:
            if stone.pk in reports_by_stone:
                continue
            detail = StoneSerializer(stone, context=self.get_serializer_context()).data
            rows.append(
                feed_row(
                    kind="stone",
                    record_id=stone.pk,
                    reference=detail.get("stone_reference")
                    or detail.get("order_reference"),
                    customer=detail.get("customer_name"),
                    type_name="Stone findings",
                    status="Awaiting findings",
                    date=stone.order.received_date,
                    waiting=True,
                    detail=detail,
                )
            )
        return paginated_workflow_feed(self, rows, request)


class InstrumentUsedViewSet(BaseModelViewSet, viewsets.ModelViewSet):
    """CRUD over the instruments recorded against a report."""

    queryset = InstrumentUsed.objects.select_related("report", "instrument")
    serializer_class = InstrumentUsedSerializer

    filter_fields = ("report", "instrument")
    ordering_fields = ("id", "created_at")

    @transaction.atomic
    def perform_create(self, serializer):
        """Save the instrument and its certificate snapshot as one change."""
        report = serializer.validated_data["report"]
        self._assert_report_editable(report, self.request.user)
        serializer.save()
        if report.is_finalized:
            refresh_certificate_snapshot(report, user=self.request.user)

    @transaction.atomic
    def perform_update(self, serializer):
        """Save the instrument and its certificate snapshot as one change."""
        previous_report = serializer.instance.report
        report = serializer.validated_data.get("report", previous_report)
        self._assert_report_editable(previous_report, self.request.user)
        self._assert_report_editable(report, self.request.user)
        serializer.save()
        for affected_report in {previous_report, report}:
            if affected_report.is_finalized:
                refresh_certificate_snapshot(affected_report, user=self.request.user)

    @transaction.atomic
    def perform_destroy(self, instance):
        """Remove the instrument and refresh the certificate atomically."""
        report = instance.report
        self._assert_report_editable(report, self.request.user)
        super().perform_destroy(instance)
        if report.is_finalized:
            refresh_certificate_snapshot(report, user=self.request.user)

    @staticmethod
    def _assert_report_editable(report, user=None) -> None:
        """Only the correction permission may pass the finalized lock.

        Without this the readings could be rewritten after the report they
        belong to was locked, which would make the lock meaningless.
        """
        if report.is_finalized and (
            user is None or not user.has_perm("identification.edit_finalized_report")
        ):
            raise ServiceError("A finalized report cannot be edited.")
