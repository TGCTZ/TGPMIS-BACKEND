"""The two report pages share section-based result and export endpoints."""

from django.urls import path

from .views import (
    ReportCatalogView,
    ReportExportView,
    ReportHighlightsExportView,
    ReportView,
)

urlpatterns = [
    path("reports/", ReportCatalogView.as_view(), name="report-catalog"),
    path(
        "reports/financial/",
        ReportView.as_view(kind="financial"),
        name="financial-report",
    ),
    path(
        "reports/operational/",
        ReportView.as_view(kind="operational"),
        name="operational-report",
    ),
    path(
        "reports/financial/export/",
        ReportExportView.as_view(kind="financial"),
        name="financial-report-export",
    ),
    path(
        "reports/operational/export/",
        ReportExportView.as_view(kind="operational"),
        name="operational-report-export",
    ),
    path(
        "reports/financial/highlights/export/",
        ReportHighlightsExportView.as_view(kind="financial"),
        name="financial-highlights-export",
    ),
    path(
        "reports/operational/highlights/export/",
        ReportHighlightsExportView.as_view(kind="operational"),
        name="operational-highlights-export",
    ),
]
