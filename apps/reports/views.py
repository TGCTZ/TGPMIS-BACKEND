"""Thin read-only endpoints for permission-scoped reports and downloads."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.http import HttpResponse
from django.utils import timezone

from apps.core.pagination import StandardPagination

from .definitions import REPORT_TIMEZONE, REPORT_TITLES, allowed_sections, section_info
from .exports import export_highlights, export_report
from .permissions import CanReadReport
from .selectors import report_columns, report_filters, report_rows, report_sections
from .serializers import (
    ReportCatalogSerializer,
    ReportQuerySerializer,
    ReportResultSerializer,
)


class ReportCatalogView(APIView):
    """Discover allowed pages without leaking inaccessible sections."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=ReportCatalogSerializer(many=True))
    def get(self, request):
        """Return only pages having an accessible section."""
        pages = []
        for kind, title in REPORT_TITLES.items():
            sections = allowed_sections(request.user, kind)
            if sections:
                pages.append(
                    {
                        "key": kind,
                        "title": title,
                        "sections": [section_info(key) for key in sections],
                    }
                )
        return Response(pages)


class ReportView(APIView):
    """Summaries span every matching row; pagination only affects the detail table."""

    permission_classes = [CanReadReport]
    kind = None

    def report_data(self, request):
        """Validate filters and prepare authorized queries for either output."""
        serializer = ReportQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        filters = serializer.validated_data
        keys = allowed_sections(request.user, self.kind)
        selected = filters.get("section", keys[0])
        if selected not in keys:
            raise PermissionDenied("You cannot read this report section.")
        if self.kind == "financial" and filters.get("customer"):
            raise ValidationError(
                {"customer": "Customer filters belong to Operational reports."}
            )
        if self.kind == "financial" and filters.get("stone_type"):
            raise ValidationError(
                {"stone_type": "This filter belongs to Operational reports."}
            )
        if self.kind == "operational" and filters.get("status"):
            raise ValidationError("Bill status belongs to Financial reports.")
        sections = report_sections(keys, filters)
        choices = report_filters(keys)
        applied_filters = {}
        for name in ("customer", "status", "stone_type"):
            if name not in filters:
                continue
            value = filters[name]
            if name == "status":
                applied_filters["Bill status"] = value.replace("_", " ").title()
            else:
                label, category = {
                    "customer": ("Customer", "customers"),
                    "stone_type": ("Stone type", "stone_types"),
                }[name]
                applied_filters[label] = next(
                    (
                        choice["label"]
                        for choice in choices[category]
                        if choice["id"] == value
                    ),
                    str(value),
                )
        report = {
            "report": self.kind,
            "title": REPORT_TITLES[self.kind],
            "range": {
                "from": (
                    "All dates"
                    if filters.get("search", "").strip()
                    else filters["from_date"].isoformat()
                ),
                "to": (
                    "All dates"
                    if filters.get("search", "").strip()
                    else filters["to_date"].isoformat()
                ),
                "timezone": str(REPORT_TIMEZONE),
            },
            "generated_at": timezone.now(),
            "applied_filters": applied_filters,
            "filters": choices,
        }
        return report, sections, selected

    @extend_schema(parameters=[ReportQuerySerializer], responses=ReportResultSerializer)
    def get(self, request):
        """Use the shared pagination envelope consumed by all frontend tables."""
        report, sections, selected = self.report_data(request)
        pagination = StandardPagination()
        page = pagination.paginate_queryset(
            sections[selected]["queryset"], request, view=self
        )
        response = pagination.get_paginated_response(report_rows(selected, page))
        response.data.update(
            {
                **report,
                "sections": [section["summary"] for section in sections.values()],
                "section": selected,
                "columns": report_columns(selected),
            }
        )
        return Response(ReportResultSerializer(response.data).data)


class ReportExportView(ReportView):
    """Export all authorized sections, irrespective of the selected detail page."""

    @extend_schema(
        parameters=[ReportQuerySerializer],
        responses={
            (200, "application/pdf"): OpenApiTypes.BINARY,
            (
                200,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ): OpenApiTypes.BINARY,
        },
    )
    def get(self, request):
        """Return an attachment with the same filters and calculations as the screen."""
        report, sections, _ = self.report_data(request)
        file_type = request.query_params.get("file_type", "xlsx")
        content = export_report(report, sections, file_type=file_type)
        content_type = (
            "application/pdf"
            if file_type == "pdf"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response = HttpResponse(content, content_type=content_type)
        filename = (
            f"{self.kind}-reports-{report['range']['from']}-"
            f"{report['range']['to']}.{file_type}"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class ReportHighlightsExportView(ReportView):
    """Download the same authorized totals as one highlights card group."""

    @extend_schema(
        parameters=[ReportQuerySerializer],
        responses={
            (200, "application/pdf"): OpenApiTypes.BINARY,
            (
                200,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ): OpenApiTypes.BINARY,
        },
    )
    def get(self, request):
        """Render filtered summary values as an attachment."""
        report, sections, _ = self.report_data(request)
        file_type = request.query_params.get("file_type", "xlsx")
        content = export_highlights(report, sections, file_type=file_type)
        content_type = (
            "application/pdf"
            if file_type == "pdf"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response = HttpResponse(content, content_type=content_type)
        filename = (
            f"{self.kind}-highlights-{report['range']['from']}-"
            f"{report['range']['to']}.{file_type}"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
