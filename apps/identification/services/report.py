"""Identification report creation and finalization."""

from django.db import transaction
from django.utils import timezone

from apps.certificates.services import refresh_certificate_snapshot
from apps.core.exceptions import ServiceError
from apps.core.services import reference_number_for_order
from apps.gems.enums import BillStatus
from apps.orders.services import update_stone

from ..models import IdentificationReport


def _assert_payment_settled(stone) -> None:
    """Refuse findings until the stone's order is paid for.

    The gate lives here rather than in the view because it is a business rule,
    and an API exposes more than one route to a report. Reached by attribute
    traversal (``stone.order.bill``) rather than by importing ``apps.billing``,
    which sits at the same layer and must not be imported from here.

    Raises:
        ServiceError: If the order has no bill, or it is not settled.
    """
    bill = getattr(stone.order, "bill", None)
    if bill is None or bill.status != BillStatus.PAID:
        raise ServiceError("The bill must be paid before recording the findings.")


# Weight is a finding, but it lives on the Stone: the certificate snapshots the
# stone, and two places to record one number is one place to disagree. The
# findings form carries it and hands it straight on, inside this transaction.
#
# No permission check accompanies it, unlike the nested customer write on orders.
# That one creates a row of another model, which is a real route around
# ``orders.add_customer``; this writes one column of the stone the report is
# already about, and that column is a finding this endpoint exists to record.
_STONE_FIELDS = ("stone_type", "weight", "weight_unit")


def _pop_stone_fields(fields: dict) -> dict:
    """Split off the submitted fields that belong to the stone, not the report."""
    return {name: fields.pop(name) for name in _STONE_FIELDS if name in fields}


def _assert_not_already_reported(stone) -> None:
    """Refuse a second live report for a stone.

    The constraint behind this is conditional - one report per stone *among
    those not discarded* - so the database would answer with an IntegrityError
    and a generic 400. Checked here so the caller is told which stone and what
    to do instead, which is to open the report the stone already has.
    """
    if IdentificationReport.objects.filter(stone=stone).exists():
        raise ServiceError(
            f"Stone {stone.label} already has a report. Edit that one instead."
        )


@transaction.atomic
def create_report(*, stone, user=None, **fields) -> IdentificationReport:
    """Create an identification report for a stone.

    The findings are recorded after payment, so the stone's status is
    deliberately left alone - the pipeline moves it, not the report.

    Args:
        stone: The stone being identified.
        user: The gemmologist, recorded as the identifier.
        **fields: Any report field - species, colour, refractive index and so
            on - plus ``weight``/``weight_unit``, which are applied to the stone.

    Raises:
        ServiceError: If the stone's bill is unsettled, or it already has a
            report that has not been discarded.
    """
    _assert_payment_settled(stone)
    _assert_not_already_reported(stone)
    stone_fields = _pop_stone_fields(fields)

    report = IdentificationReport(
        stone=stone,
        # Findings share the order's sequence, with the stone label preserving
        # a distinct reference for every stone in that order.
        report_number=reference_number_for_order(
            stone.order, "TGC", stone_label=stone.label
        ),
        **fields,
    )
    if user is not None:
        report.created_by = user
        report.identified_by = user
    report.save()

    if stone_fields:
        update_stone(stone, user=user, **stone_fields)
    return report


@transaction.atomic
def update_report(
    report: IdentificationReport, *, user=None, **fields
) -> IdentificationReport:
    """Update a report's findings.

    Args:
        report: The report to update.
        user: The acting gemmologist.
        **fields: Any report field, plus ``weight``/``weight_unit``, which are
            applied to the stone.

    Raises:
        ServiceError: If the report is finalized and the caller lacks the
            dedicated correction permission, or a correction would remove a
            fact required by the issued certificate.
    """
    can_edit_finalized = bool(
        report.is_finalized
        and user is not None
        and user.has_perm("identification.edit_finalized_report")
    )
    if report.is_finalized and not can_edit_finalized:
        raise ServiceError("A finalized report cannot be edited.")
    _assert_payment_settled(report.stone)
    stone_fields = _pop_stone_fields(fields)

    for name, value in fields.items():
        setattr(report, name, value)
    if user is not None:
        report.updated_by = user
    report.save()

    if stone_fields:
        update_stone(report.stone, user=user, **stone_fields)
    if can_edit_finalized:
        missing = _missing_for_finalize(report)
        if missing:
            raise ServiceError(
                "A finalized report must retain the {}.".format(", ".join(missing))
            )
        refresh_certificate_snapshot(report, user=user)
    return report


#: What a report must answer before it can be locked, as ``(accessor, label)``.
#:
#: Deliberately short. The form stays permissive so a sitting at the bench can be
#: saved half-done, which means this is the only place completeness is ever
#: checked - and a certificate quotes these four: what the stone is, what it
#: looks like, how big it is, and the verdict. Everything else is situational; a
#: stone may legitimately defeat a test and still deserve a certificate.
#:
#: ``weight`` is read through the stone, not the report: the form collects it
#: alongside the findings but ``_pop_stone_fields`` writes it to the stone, which
#: is also where :func:`apps.certificates.services.issue_certificate` looks.
FINALIZE_REQUIRED_FIELDS = (
    ("species", "species"),
    ("color", "colour"),
    ("stone.weight", "weight"),
    ("conclusion", "comments"),
    ("stone.photo", "photograph"),
)


def _missing_for_finalize(report: IdentificationReport) -> list[str]:
    """Labels of the required findings this report has not answered yet."""
    missing = []
    for accessor, label in FINALIZE_REQUIRED_FIELDS:
        value = report
        for part in accessor.split("."):
            value = getattr(value, part, None)
        # A blank CharField is "" and an unset FK or decimal is None; both mean
        # unanswered, so falsiness is the right test for all four.
        if not value:
            missing.append(label)
    return missing


def finalize_report(
    report: IdentificationReport, *, user=None, verified_by=None
) -> IdentificationReport:
    """Lock a report against further edits, naming both gemmologists.

    One-way: there is no un-finalize service. A later correction is a privileged
    edit of the findings and existing certificate snapshot; it does not erase
    the original sign-off.

    ``verified_by`` is the second signatory. Asked for here rather than while the
    report is being written because it is a sign-off, not a finding - and this is
    the moment the document stops being a draft. Optional at the service level so
    a report can still be closed when only one gemmologist saw the stone; the
    certificate then prints one name.

    Args:
        report: The report to lock.
        user: The gemmologist finalizing it, recorded as the identifier.
        verified_by: The second gemmologist, who checked the findings.

    Raises:
        ServiceError: If the report is already finalized, if required findings
            are still blank, or if the same person is named as both
            gemmologists.
    """
    if report.is_finalized:
        raise ServiceError("Report is already finalized.")

    # Every missing field at once, not the first one: a gemmologist away from the
    # bench should learn everything still outstanding in a single round trip.
    missing = _missing_for_finalize(report)
    if missing:
        raise ServiceError("Record the {} before finalizing.".format(", ".join(missing)))

    # A second opinion from the same head is not a second opinion. Caught here
    # rather than in the serializer because it is the rule the document's own
    # "examined by at least two qualified Gemmologists" claim rests on.
    if verified_by is not None and user is not None and verified_by.pk == user.pk:
        raise ServiceError("The second gemmologist must be a different person.")

    report.is_finalized = True
    report.identified_at = timezone.now()
    if verified_by is not None:
        report.verified_by = verified_by
    if user is not None:
        report.identified_by = user
        report.updated_by = user
    report.save(
        update_fields=[
            "is_finalized",
            "identified_at",
            "identified_by",
            "verified_by",
            "updated_at",
            "updated_by",
        ]
    )
    return report
