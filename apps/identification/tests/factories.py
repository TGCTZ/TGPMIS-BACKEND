"""Helpers for building reports that tests can actually finalize."""

from decimal import Decimal

from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.gems.enums import NatureType, OpticCharacter, Transparency
from apps.gems.tests.factories import (
    ColorFactory,
    ShapeCutFactory,
    SpeciesFactory,
    VarietyFactory,
)
from apps.identification.services import create_report, finalize_report
from apps.users.tests.factories import UserFactory


def second_gemmologist():
    """Create an active second signatory eligible for the finalize endpoint."""
    account = UserFactory()
    group, _ = Group.objects.get_or_create(name="gemmologist")
    account.groups.add(group)
    return account


def finalize_complete_report(report, user):
    """Finalize a complete report with an independent second signatory."""
    return finalize_report(report, user=user, verified_by=second_gemmologist())


def create_finalizable_report(stone, user, **overrides):
    """A report answering every field ``finalize_report`` insists on.

    Most tests care about what happens *after* a report is locked, not about the
    completeness rule itself - so they would otherwise repeat the same findings
    and photo setup, and each break the next time that list changes. The rule is
    tested on its own, in ``test_finalize_needs_the_required_findings``.

    Args:
        stone: The stone to report on. Its order must be paid.
        user: The gemmologist recorded as the identifier.
        **overrides: Any report field to set instead of the default.
    """
    if not stone.photo:
        stone.photo.save(
            "stone.gif",
            SimpleUploadedFile(
                "stone.gif",
                b"GIF89a\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00"
                b"\x01\x00\x01\x00\x00\x02\x00;",
                content_type="image/gif",
            ),
            save=True,
        )

    species = SpeciesFactory()
    fields = {
        "species": species,
        "variety": VarietyFactory(species=species),
        "color": ColorFactory(),
        "shape_cut": ShapeCutFactory(),
        "weight": Decimal("2.500"),
        "transparency": Transparency.TRANSPARENT,
        "optic_character": OpticCharacter.DR,
        "nature_type": NatureType.NATURAL,
        "conclusion": "Natural ruby.",
        **overrides,
    }
    return create_report(stone=stone, user=user, **fields)
