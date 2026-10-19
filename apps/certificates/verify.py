"""Public certificate verification: the page behind the QR code on the paper.

A plain Django view mounted outside ``/api/v1/``, for the same reasons the GePG
webhooks are (see ``apps/billing/webhooks.py``):

1. The URL is **printed on a physical document**. A certificate issued today may
   be checked in ten years, long after ``/api/v2/`` exists. A versioned prefix
   would strand every certificate already in circulation.
2. It is unauthenticated by design. ``DEFAULT_PERMISSION_CLASSES =
   [IsAuthenticated]`` fails closed, and reaching around it per view inside DRF
   is exactly the drift that setting exists to prevent. Staying outside DRF
   keeps that guarantee whole.
3. It answers with HTML for a phone camera, not JSON for a client.

What it deliberately does **not** show: the customer. The printed certificate
names no owner, and a public endpoint keyed on a number found on a document must
not turn that number into a way to look people up. Findings describe the stone;
they identify nobody.
"""

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from apps.core.services import format_reference_number
from apps.gems.enums import CertificateStatus, WeightUnit

from .models import Certificate
from .services.assets import asset_data_uri, photo_data_uri

TEMPLATE = "certificates/verify.html"


class CertificateVerifyView(View):
    """Confirm, to anyone holding a certificate, whether it stands."""

    def get(self, request, certificate_number: str, *args, **kwargs) -> HttpResponse:
        """Look up a certificate by its printed number.

        An unknown number and a revoked certificate are both answered plainly
        rather than with a 404 page - whoever scanned this is standing in front
        of a piece of paper and needs to be told what it is worth, not shown a
        stack trace or a generic error.
        """
        header = {
            "lab_name": settings.CERTIFICATE_LAB_NAME,
            "ministry_name": settings.CERTIFICATE_MINISTRY_NAME,
            "coat_of_arms": asset_data_uri("coat_of_arms"),
            "tgc_logo": asset_data_uri("tgc_logo"),
        }
        certificate = (
            Certificate.objects.select_related("stone", "stone__order", "report")
            .filter(certificate_number=certificate_number)
            .first()
        )

        if certificate is None:
            return render(
                request,
                TEMPLATE,
                {
                    "certificate_number": format_reference_number(certificate_number),
                    "found": False,
                    **header,
                },
                status=404,
            )

        is_revoked = certificate.status == CertificateStatus.REVOKED
        return render(
            request,
            TEMPLATE,
            {
                "found": True,
                "certificate": certificate,
                "certificate_number": format_reference_number(
                    certificate.certificate_number
                ),
                "is_revoked": is_revoked,
                "weight_unit": WeightUnit(certificate.weight_unit_snapshot).symbol,
                "photo": (
                    photo_data_uri(certificate.photo_snapshot)
                    or photo_data_uri(certificate.stone.photo)
                ),
                "report_number": (
                    format_reference_number(
                        certificate.report_number_snapshot
                        or certificate.report.report_number
                    )
                ),
                **header,
            },
            # A revoked certificate is a successful lookup of a document that no
            # longer stands, not a failed one. 200 with a loud banner.
            status=200,
        )
