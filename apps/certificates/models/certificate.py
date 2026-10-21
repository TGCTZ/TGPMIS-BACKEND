"""Certificate: one per stone, the document the customer leaves with."""

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import BaseModel
from apps.gems.enums import CertificateStatus, WeightUnit


class Certificate(BaseModel):
    """A certificate for one stone.

    The snapshot fields are deliberate denormalization, not an oversight. A
    certificate is a statement made on a date. Lookup renames do not change its
    snapshots; an authorized finalized-report correction is the explicit path
    for refreshing those facts in place.
    """

    stone = models.OneToOneField(
        "orders.Stone", on_delete=models.PROTECT, related_name="certificate"
    )
    report = models.ForeignKey(
        "identification.IdentificationReport",
        on_delete=models.PROTECT,
        related_name="certificates",
    )
    certificate_number = models.CharField(max_length=30)

    # Frozen at issue time.
    stone_type_snapshot = models.CharField(max_length=100)
    weight_snapshot = models.DecimalField(max_digits=10, decimal_places=3)
    # Snapshotted alongside the weight: a certificate that states a number
    # without its unit states nothing, and reading the stone's live unit later
    # would let an edit rewrite an issued document.
    weight_unit_snapshot = models.CharField(
        max_length=10, choices=WeightUnit.choices, default=WeightUnit.CARAT
    )
    color_snapshot = models.CharField(max_length=100, blank=True, default="")
    origin_snapshot = models.CharField(max_length=100, blank=True, default="")

    # The rest of what the printed certificate states.
    #
    # Every one of these is reachable through `self.report`, and none of them is
    # read that way on purpose. A lookup row renamed years later, or a finding
    # corrected after the fact, must not rewrite a document already in a
    # customer's hands - so the certificate keeps its own copy of every word it
    # prints. The report FK stays for provenance, not for rendering.
    species_snapshot = models.CharField(max_length=100, blank=True, default="")
    variety_snapshot = models.CharField(max_length=100, blank=True, default="")
    shape_cut_snapshot = models.CharField(max_length=100, blank=True, default="")
    # Enum labels, not codes: the document is read by a customer, not a program.
    transparency_snapshot = models.CharField(max_length=50, blank=True, default="")
    optic_character_snapshot = models.CharField(max_length=50, blank=True, default="")
    treatment_snapshot = models.CharField(max_length=100, blank=True, default="")
    nature_type_snapshot = models.CharField(max_length=50, blank=True, default="")
    refractive_index_snapshot = models.CharField(max_length=50, blank=True, default="")
    # Held as text to preserve the exact finding; numeric readings are formatted
    # for display without rewriting this snapshot.
    specific_gravity_snapshot = models.CharField(max_length=50, blank=True, default="")
    comments_snapshot = models.TextField(blank=True, default="")
    # ``[{"name": ..., "used": bool}, ...]``: the full instrument checklist as
    # it stood at issue. Certificates issued before the checklist hold
    # ``[{"name": ..., "reading": ...}]`` with every row used. A list
    # rather than rows, because these are words on a document rather than a
    # relation anyone queries.
    instruments_snapshot = models.JSONField(default=list, blank=True)
    # The report number as printed (REPORT NO on the document). Copied because
    # the report can, in principle, be renumbered; the paper cannot.
    report_number_snapshot = models.CharField(max_length=50, blank=True, default="")

    # Both signatories, as names. The FKs live on the report; these are the
    # words that were printed, and they survive a user being renamed.
    gemmologist = models.CharField(max_length=100, blank=True, default="")
    gemmologist_two = models.CharField(max_length=100, blank=True, default="")

    # The bench photograph as it was at issue.
    #
    # Holds the stored path the stone's photo had at that moment. Re-photographing
    # the stone writes a *new* file and repoints the stone, leaving this one
    # untouched - so the certificate keeps the picture it was issued with without
    # duplicating the bytes.
    photo_snapshot = models.ImageField(
        upload_to="certificates/stones/", blank=True, null=True
    )

    status = models.CharField(
        max_length=20,
        choices=CertificateStatus.choices,
        default=CertificateStatus.ISSUED,
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    issued_at = models.DateTimeField()

    class Meta:
        ordering = ["-issued_at"]
        permissions = [
            ("issue_certificate", "Can issue a certificate"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["certificate_number"],
                condition=Q(deleted_at__isnull=True),
                name="%(app_label)s_%(class)s_unique_certificate_number",
            ),
        ]

    def __str__(self) -> str:
        return self.certificate_number
