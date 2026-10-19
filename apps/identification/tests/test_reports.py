"""Findings, the payment gate and the finalize lock."""

import re
from decimal import Decimal

import pytest

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.billing.dev import simulate_payment
from apps.billing.services import generate_bill_for_order
from apps.certificates.models import Certificate
from apps.certificates.selectors import certification_worklist
from apps.certificates.services import issue_certificate
from apps.certificates.services.pdf import certificate_context
from apps.core.exceptions import ServiceError
from apps.core.services import format_reference_number, reference_number_for_order
from apps.gems.enums import BillStatus, NatureType, WeightUnit
from apps.gems.tests.factories import (
    ColorFactory,
    InstrumentFactory,
    SpeciesFactory,
    StoneCategoryFactory,
    StoneTypeFactory,
)
from apps.identification.models import IdentificationReport, InstrumentUsed
from apps.identification.selectors import findings_worklist
from apps.identification.services import create_report, finalize_report, update_report
from apps.identification.tests.factories import create_finalizable_report
from apps.orders.serializers import StoneSerializer
from apps.orders.services import add_stone
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


def test_nature_type_choices_are_limited_to_the_lab_values():
    """Only the three supported nature classifications can be recorded."""
    assert NatureType.values == ["natural", "artificial", "synthetic"]


@pytest.fixture
def paid_stone(settings):
    """A stone whose order has been billed and settled."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("50000.00")))
    simulate_payment(generate_bill_for_order(order))
    stone.refresh_from_db()
    return stone


@pytest.fixture
def billed_stone(settings):
    """A stone billed but not yet paid for."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("50000.00")))
    generate_bill_for_order(order)
    stone.refresh_from_db()
    return stone


def test_create_report_allocates_a_number(paid_stone, user):
    """The service derives an unpadded report reference from the order."""
    report = create_report(stone=paid_stone, user=user)

    assert re.fullmatch(r"TGC-\d{4}-\d+-\d+", report.report_number)
    report_year, report_sequence, stone_label = report.report_number.split("-")[1:]
    order_year, order_sequence = paid_stone.order.reference_number.split("-")[1:]
    assert report_year == order_year
    assert int(report_sequence) == int(order_sequence)
    assert stone_label == paid_stone.label == "1"
    assert report.identified_by == user
    assert not report.is_finalized


def test_identification_is_refused_before_payment(billed_stone):
    """Work starts only once the customer has paid.

    The gate lives in the service rather than the view, because an API offers
    more than one route to a report.
    """
    with pytest.raises(ServiceError, match="bill must be paid"):
        create_report(stone=billed_stone)


def test_identification_is_refused_when_the_order_has_no_bill():
    """A stone that was never billed cannot be worked on either."""
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("1000.00")))

    with pytest.raises(ServiceError, match="bill must be paid"):
        create_report(stone=stone)


def test_a_draft_report_can_be_edited(paid_stone, user):
    """Findings are built up over a sitting, so a draft stays open."""
    report = create_report(stone=paid_stone, user=user)
    species = SpeciesFactory(name="Corundum")

    update_report(report, user=user, species=species, refractive_index="1.762-1.770")

    report.refresh_from_db()
    assert report.species == species
    assert report.refractive_index == "1.762-1.770"


def test_finalizing_locks_the_report(paid_stone, user):
    """The lock is one-way and stamps who signed it off."""
    report = create_finalizable_report(paid_stone, user)
    finalize_report(report, user=user)

    report.refresh_from_db()
    assert report.is_finalized
    assert report.identified_at is not None
    assert report.identified_by == user


def test_a_finalized_report_cannot_be_edited(paid_stone, user):
    """The strongest guard in the system.

    A certificate quotes the report, so findings that could still change after
    finalization would make an issued certificate a claim about nothing.
    """
    report = create_finalizable_report(paid_stone, user)
    finalize_report(report, user=user)

    with pytest.raises(ServiceError, match="finalized report cannot be edited"):
        update_report(report, conclusion="Actually, no.")


def test_a_report_cannot_be_finalized_twice(paid_stone, user):
    """There is no un-finalize, so a second call is a mistake worth reporting."""
    report = create_finalizable_report(paid_stone, user)
    finalize_report(report, user=user)

    with pytest.raises(ServiceError, match="already finalized"):
        finalize_report(report, user=user)


def test_findings_worklist_holds_paid_unfinalized_stones(paid_stone, billed_stone, user):
    """The queue is exactly the bench's inbox."""
    assert paid_stone in findings_worklist()
    assert billed_stone not in findings_worklist()

    report = create_finalizable_report(paid_stone, user)
    assert paid_stone in findings_worklist(), "a draft is still work in progress"

    finalize_report(report, user=user)
    assert paid_stone not in findings_worklist()


def test_report_endpoint_creates_via_the_service(paid_stone, admin_user, auth_client):
    """POST mints the number and ignores a client-supplied one."""
    original_order_reference = paid_stone.order.reference_number
    response = auth_client(admin_user).post(
        "/api/v1/identification-reports/",
        {
            "stone": paid_stone.pk,
            "report_number": "CLIENT-SUPPLIED",
            "conclusion": "Natural ruby.",
            "color": ColorFactory().pk,
        },
    )

    assert response.status_code == 201, response.data
    assert re.fullmatch(r"TGC-\d{2}/\d{2}-\d+-1", response.data["report_number"])
    assert response.data["stone_reference"] == (
        format_reference_number(
            reference_number_for_order(paid_stone.order, "ORD", stone_label="1")
        )
    )
    # New paperwork uses the unpadded sequence without rewriting the order row.
    paid_stone.order.refresh_from_db()
    assert paid_stone.order.reference_number == original_order_reference
    assert response.data["identified_by_label"] is not None


def test_report_endpoint_refuses_before_payment(billed_stone, admin_user, auth_client):
    """The service's refusal surfaces as a 400 carrying its message."""
    response = auth_client(admin_user).post(
        "/api/v1/identification-reports/", {"stone": billed_stone.pk}
    )

    assert response.status_code == 400
    assert "bill must be paid" in str(response.data)


def test_finalize_endpoint_requires_the_finalize_permission(
    paid_stone, viewer_user, auth_client
):
    """Signing off findings is the gemmologist's job, not reception's."""
    report = create_report(stone=paid_stone)
    response = auth_client(viewer_user).post(
        f"/api/v1/identification-reports/{report.pk}/finalize/"
    )

    assert response.status_code == 403
    report.refresh_from_db()
    assert not report.is_finalized


def test_finalize_endpoint_issues_a_certificate_automatically(
    paid_stone, admin_user, auth_client
):
    """Finalizing paid findings issues the stone's certificate in the same action."""
    report = create_finalizable_report(paid_stone, None)

    response = auth_client(admin_user).post(
        f"/api/v1/identification-reports/{report.pk}/finalize/"
    )

    assert response.status_code == 200, response.data
    report.refresh_from_db()
    certificate = Certificate.objects.get(stone=paid_stone)
    assert report.is_finalized
    assert certificate.report == report
    assert certificate.issued_by == admin_user


def test_finalize_endpoint_keeps_the_paid_bill_requirement(
    paid_stone, admin_user, auth_client
):
    """An unpaid bill rolls back both finalization and automatic issuance."""
    report = create_finalizable_report(paid_stone, None)
    bill = paid_stone.order.bill
    bill.status = BillStatus.PARTIALLY_PAID
    bill.save(update_fields=["status"])

    response = auth_client(admin_user).post(
        f"/api/v1/identification-reports/{report.pk}/finalize/"
    )

    assert response.status_code == 400
    report.refresh_from_db()
    assert not report.is_finalized
    assert not Certificate.objects.filter(stone=paid_stone).exists()


def test_finalized_reports_are_listed_only_in_the_finalized_feed(
    paid_stone, admin_user, auth_client
):
    """The normal findings queue stays focused on open work."""
    report = create_finalizable_report(paid_stone, admin_user)
    finalize_report(report, user=admin_user)
    client = auth_client(admin_user)

    open_work = client.get("/api/v1/identification-reports/workflow-feed/")
    finalized = client.get(
        "/api/v1/identification-reports/workflow-feed/", {"status": "Finalized"}
    )

    assert open_work.status_code == 200
    assert open_work.data["results"] == []
    assert finalized.status_code == 200
    assert len(finalized.data["results"]) == 1
    assert finalized.data["results"][0]["status"] == "Finalized"
    assert finalized.data["results"][0]["record_id"] == report.pk


def test_patching_a_finalized_report_is_refused(paid_stone, admin_user, auth_client):
    """The lock holds over the API, not just in the service."""
    report = create_finalizable_report(paid_stone, None)
    client = auth_client(admin_user)
    client.post(f"/api/v1/identification-reports/{report.pk}/finalize/")

    response = client.patch(
        f"/api/v1/identification-reports/{report.pk}/",
        {"conclusion": "Rewritten after the fact."},
    )

    assert response.status_code == 400
    report.refresh_from_db()
    assert report.conclusion == "Natural ruby."


@pytest.mark.parametrize("role_name", ["admin", "superadmin"])
def test_admin_correction_updates_snapshot_and_preserves_issuance(
    paid_stone, roles, gemmologist_user, auth_client, role_name
):
    """A correction changes the certified facts, not when or by whom it was issued."""
    from django.contrib.auth.models import Group

    from apps.certificates.services import issue_certificate
    from apps.gems.enums import CertificateStatus
    from apps.users.tests.factories import UserFactory

    admin = UserFactory()
    admin.groups.add(Group.objects.get(name=role_name))
    second_gemmologist = UserFactory()
    second_gemmologist.groups.add(Group.objects.get(name="gemmologist"))
    report = create_finalizable_report(paid_stone, gemmologist_user)
    finalize_report(report, user=gemmologist_user, verified_by=second_gemmologist)
    certificate = issue_certificate(paid_stone, user=admin)
    replacement_type = StoneTypeFactory(category=paid_stone.stone_category)
    original = {
        "number": certificate.certificate_number,
        "issued_at": certificate.issued_at,
        "issued_by": certificate.issued_by_id,
        "gemmologist": certificate.gemmologist,
        "gemmologist_two": certificate.gemmologist_two,
    }

    response = auth_client(admin).patch(
        f"/api/v1/identification-reports/{report.pk}/",
        {
            "conclusion": "Corrected finding.",
            "weight": "3.125",
            "stone_type": replacement_type.pk,
        },
    )

    assert response.status_code == 200, response.data
    report.refresh_from_db()
    paid_stone.refresh_from_db()
    certificate.refresh_from_db()
    assert report.conclusion == "Corrected finding."
    assert paid_stone.weight == Decimal("3.125")
    assert paid_stone.stone_type_id == replacement_type.pk
    assert certificate.comments_snapshot == report.conclusion
    assert certificate.weight_snapshot == paid_stone.weight
    assert certificate.stone_type_snapshot == replacement_type.name
    assert certificate.updated_by == admin
    assert report.updated_by == admin
    assert certificate.certificate_number == original["number"]
    assert certificate.issued_at == original["issued_at"]
    assert certificate.issued_by_id == original["issued_by"]
    assert certificate.gemmologist == original["gemmologist"]
    assert certificate.gemmologist_two == original["gemmologist_two"]
    assert certificate.status == CertificateStatus.ISSUED

    instrument = InstrumentFactory()
    instrument_response = auth_client(admin).post(
        "/api/v1/instruments-used/",
        {"report": report.pk, "instrument": instrument.pk},
    )
    assert instrument_response.status_code == 201, instrument_response.data
    certificate.refresh_from_db()
    used_names = {
        item["name"] for item in certificate.instruments_snapshot if item["used"]
    }
    assert used_names == {instrument.name}

    from django.core.files.uploadedfile import SimpleUploadedFile

    photo = SimpleUploadedFile(
        "corrected.gif",
        b"GIF89a\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00"
        b"\x01\x00\x01\x00\x00\x02\x00;",
        content_type="image/gif",
    )
    photo_response = auth_client(admin).patch(
        f"/api/v1/stones/{paid_stone.pk}/", {"photo": photo}, format="multipart"
    )
    assert photo_response.status_code == 200, photo_response.data
    paid_stone.refresh_from_db()
    certificate.refresh_from_db()
    assert certificate.photo_snapshot.name == paid_stone.photo.name
    assert certificate_context(certificate)["certificate"].comments_snapshot == (
        "Corrected finding."
    )

    verification = auth_client(admin).get(f"/verify/{certificate.certificate_number}/")
    assert verification.status_code == 200
    verification_html = verification.content.decode()
    assert replacement_type.name in verification_html
    assert "3.125" in verification_html


def test_gemmologist_cannot_edit_a_finalized_report(
    paid_stone, gemmologist_user, auth_client
):
    """The bench can finalize findings but cannot rewrite their sign-off."""
    report = create_finalizable_report(paid_stone, gemmologist_user)
    finalize_report(report, user=gemmologist_user)

    response = auth_client(gemmologist_user).patch(
        f"/api/v1/identification-reports/{report.pk}/",
        {"conclusion": "Unauthorized correction."},
    )

    assert response.status_code == 400
    report.refresh_from_db()
    assert report.conclusion == "Natural ruby."


def test_failed_snapshot_refresh_rolls_back_a_correction(
    paid_stone, roles, auth_client, monkeypatch
):
    """Snapshot failure must roll back both findings and stone measurements."""
    from django.contrib.auth.models import Group

    from apps.users.tests.factories import UserFactory

    admin = UserFactory()
    admin.groups.add(Group.objects.get(name="admin"))
    report = create_finalizable_report(paid_stone, admin)
    finalize_report(report, user=admin)
    issue_certificate(paid_stone, user=admin)

    def fail_refresh(*args, **kwargs):
        raise ServiceError("Snapshot refresh failed.")

    monkeypatch.setattr(
        "apps.identification.services.report.refresh_certificate_snapshot",
        fail_refresh,
    )
    response = auth_client(admin).patch(
        f"/api/v1/identification-reports/{report.pk}/",
        {"conclusion": "This must roll back.", "weight": "8.000"},
    )

    assert response.status_code == 400
    report.refresh_from_db()
    paid_stone.refresh_from_db()
    assert report.conclusion == "Natural ruby."
    assert paid_stone.weight == Decimal("2.500")


def test_is_finalized_is_not_directly_writable(paid_stone, admin_user, auth_client):
    """A report must not be locked by a field write, bypassing the stamp.

    Setting the flag directly would leave identified_at and identified_by empty,
    so the certificate would name no gemmologist.
    """
    report = create_report(stone=paid_stone)
    response = auth_client(admin_user).patch(
        f"/api/v1/identification-reports/{report.pk}/", {"is_finalized": True}
    )

    assert response.status_code == 200, response.data
    report.refresh_from_db()
    assert not report.is_finalized
    assert report.identified_at is None


def test_instruments_cannot_be_added_to_a_finalized_report(
    paid_stone, admin_user, auth_client
):
    """The lock covers the instruments too, or it means nothing."""
    report = create_finalizable_report(paid_stone, None)
    client = auth_client(admin_user)

    first = client.post(
        "/api/v1/instruments-used/",
        {"report": report.pk, "instrument": InstrumentFactory().pk},
    )
    assert first.status_code == 201, first.data

    client.post(f"/api/v1/identification-reports/{report.pk}/finalize/")

    second = client.post(
        "/api/v1/instruments-used/",
        {"report": report.pk, "instrument": InstrumentFactory().pk},
    )
    assert second.status_code == 400
    assert InstrumentUsed.objects.filter(report=report).count() == 1


def test_an_instrument_can_be_ticked_only_once_per_report(
    paid_stone, admin_user, auth_client
):
    """A toggle is on or off; a double click must not record it twice."""
    report = create_finalizable_report(paid_stone, None)
    client = auth_client(admin_user)
    payload = {"report": report.pk, "instrument": InstrumentFactory().pk}

    assert client.post("/api/v1/instruments-used/", payload).status_code == 201
    assert client.post("/api/v1/instruments-used/", payload).status_code == 400


def test_findings_worklist_endpoint_lists_stones(paid_stone, admin_user, auth_client):
    """The queue endpoint returns stones, not reports."""
    response = auth_client(admin_user).get("/api/v1/identification-reports/worklist/")

    assert response.status_code == 200, response.data
    assert response.data["count"] == 1
    assert response.data["results"][0]["label"] == paid_stone.label


def test_listing_reports_does_not_n_plus_one(
    settings, admin_user, auth_client, django_assert_max_num_queries
):
    """Every lookup a report expands is joined up front."""
    settings.GEPG_SIMULATE = True
    for _ in range(5):
        order = OrderFactory(stone_count=1)
        stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("1000.00")))
        simulate_payment(generate_bill_for_order(order))
        create_report(stone=stone, species=SpeciesFactory(), color=ColorFactory())

    client = auth_client(admin_user)
    with django_assert_max_num_queries(12):
        response = client.get("/api/v1/identification-reports/")

    assert response.status_code == 200
    assert response.data["count"] == 5
    assert IdentificationReport.objects.count() == 5


def test_weight_recorded_on_the_report_lands_on_the_stone(paid_stone, user):
    """One source of truth: the certificate snapshots the stone, not the report."""
    report = create_report(stone=paid_stone, weight=Decimal("3.250"), user=user)

    paid_stone.refresh_from_db()
    assert paid_stone.weight == Decimal("3.250")
    # The report carries the value through; it does not keep a copy.
    assert not hasattr(report, "weight")


def test_a_weight_can_be_cleared_while_the_report_is_a_draft(paid_stone, user):
    """A reading typed against the wrong stone has to be removable."""
    report = create_report(stone=paid_stone, weight=Decimal("3.250"), user=user)

    update_report(report, weight=None, user=user)

    paid_stone.refresh_from_db()
    assert paid_stone.weight is None


def test_report_endpoint_records_the_weight_in_one_request(
    paid_stone, admin_user, auth_client
):
    """The findings and the weight they were measured with arrive together.

    One request, one transaction - so a stone can never end up weighed against
    findings that failed to save, or the reverse.
    """
    response = auth_client(admin_user).post(
        "/api/v1/identification-reports/",
        {
            "stone": paid_stone.pk,
            "conclusion": "Natural ruby.",
            "weight": "3.250",
            "weight_unit": WeightUnit.GRAM,
        },
    )

    assert response.status_code == 201, response.data
    assert response.data["stone_weight"] == "3.250"
    assert response.data["stone_weight_unit"] == WeightUnit.GRAM

    paid_stone.refresh_from_db()
    assert paid_stone.weight == Decimal("3.250")
    assert paid_stone.weight_unit == WeightUnit.GRAM


def test_report_endpoint_records_an_exact_type_in_the_stone(
    settings, admin_user, auth_client
):
    """Findings save the exact type on the stone without replacing its tier."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    category = StoneCategoryFactory(price=Decimal("1000.00"))
    stone = add_stone(order, stone_category=category)
    simulate_payment(generate_bill_for_order(order))
    exact_type = StoneTypeFactory(category=category)

    response = auth_client(admin_user).post(
        "/api/v1/identification-reports/",
        {"stone": stone.pk, "stone_type": exact_type.pk},
    )

    assert response.status_code == 201, response.data
    assert response.data["stone_type_detail"]["id"] == exact_type.pk
    stone.refresh_from_db()
    assert stone.stone_type_id == exact_type.pk


def test_report_endpoint_rejects_a_type_from_another_category(
    paid_stone, admin_user, auth_client
):
    """The API enforces the same type/category rule as the selector."""
    other_type = StoneTypeFactory()

    response = auth_client(admin_user).post(
        "/api/v1/identification-reports/",
        {"stone": paid_stone.pk, "stone_type": other_type.pk},
    )

    assert response.status_code == 400
    assert "belonging to this stone's category" in str(response.data)


def test_finalize_requires_exact_type_for_category_only_stone(settings, user):
    """The saved billing category is not enough to finalize findings."""
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    category = StoneCategoryFactory(price=Decimal("1000.00"))
    stone = add_stone(order, stone_category=category)
    simulate_payment(generate_bill_for_order(order))
    report = create_finalizable_report(stone, user)

    with pytest.raises(ServiceError, match="stone type"):
        finalize_report(report, user=user)


def test_a_finalized_report_refuses_a_weight_change(paid_stone, admin_user, auth_client):
    """The lock covers the stone's weight too.

    Otherwise a certificate could quote a weight that was edited after it was
    signed off.
    """
    report = create_finalizable_report(paid_stone, None, weight=Decimal("3.250"))
    client = auth_client(admin_user)
    client.post(f"/api/v1/identification-reports/{report.pk}/finalize/")

    response = client.patch(
        f"/api/v1/identification-reports/{report.pk}/", {"weight": "9.999"}
    )

    assert response.status_code == 400
    paid_stone.refresh_from_db()
    assert paid_stone.weight == Decimal("3.250")


def test_finalize_needs_the_required_findings(paid_stone, user):
    """A blank report cannot be locked, and the refusal names everything missing.

    The form stays permissive so a sitting at the bench can be saved half-done;
    this is where that permissiveness stops, because the next step after
    finalizing is a certificate quoting the findings.
    """
    report = create_report(stone=paid_stone, user=user)

    with pytest.raises(ServiceError) as refusal:
        finalize_report(report, user=user)

    # Every missing field at once, not just the first: a gemmologist should not
    # have to discover them one failed click at a time.
    message = str(refusal.value)
    for field in ("species", "colour", "weight", "comments"):
        assert field in message

    report.refresh_from_db()
    assert not report.is_finalized


def test_finalize_requires_a_stone_photograph(paid_stone, user):
    """A certificate cannot be issued from findings without its required photo."""
    report = create_finalizable_report(paid_stone, user)
    paid_stone.photo.delete(save=True)

    with pytest.raises(ServiceError, match="photograph"):
        finalize_report(report, user=user)

    report.refresh_from_db()
    assert not report.is_finalized


def test_finalize_names_only_what_is_still_missing(paid_stone, user):
    """The three that are answered drop out of the message."""
    report = create_finalizable_report(paid_stone, user, conclusion="")

    with pytest.raises(ServiceError, match="comments") as refusal:
        finalize_report(report, user=user)

    assert "species" not in str(refusal.value)


def test_a_complete_report_finalizes(paid_stone, user):
    """The rule is a gate, not a wall."""
    report = create_finalizable_report(paid_stone, user)
    finalize_report(report, user=user)

    report.refresh_from_db()
    assert report.is_finalized


def test_findings_worklist_is_searchable(paid_stone, admin_user, auth_client):
    """The queue is worked by looking for a parcel, so it must be searchable."""
    client = auth_client(admin_user)
    reference = paid_stone.order.reference_number

    hit = client.get("/api/v1/identification-reports/worklist/", {"search": reference})
    assert hit.status_code == 200
    assert [row["id"] for row in hit.data["results"]] == [paid_stone.pk]

    miss = client.get(
        "/api/v1/identification-reports/worklist/", {"search": "no-such-parcel"}
    )
    assert miss.data["results"] == []


def test_findings_worklist_row_carries_its_report(paid_stone, admin_user, auth_client):
    """A stone with a draft must be edited, not recorded again.

    ``Stone.report`` is a OneToOne, so a second create is rejected as a
    duplicate - the row has to say which of the two actions it is offering.
    """
    client = auth_client(admin_user)

    before = client.get("/api/v1/identification-reports/worklist/")
    assert before.data["results"][0]["report_detail"] is None

    report = create_report(stone=paid_stone)

    after = client.get("/api/v1/identification-reports/worklist/")
    detail = after.data["results"][0]["report_detail"]
    assert detail["id"] == report.pk
    assert detail["report_number"] == format_reference_number(report.report_number)
    assert detail["is_finalized"] is False


def test_discarding_a_report_frees_the_stone(paid_stone, user):
    """The delete dialog promises the stone returns to the queue, so it must.

    The relation is a ForeignKey with a conditional unique constraint rather
    than a OneToOne for exactly this: a OneToOne's unique index covers
    soft-deleted rows, so a discarded report would occupy its stone forever and
    the stone could never be reported on again.
    """
    first = create_report(stone=paid_stone, user=user)
    first.delete()

    assert paid_stone in findings_worklist()

    second = create_report(stone=paid_stone, user=user)
    assert second.pk != first.pk

    # The stone reads back the live report, not the discarded one.
    paid_stone.refresh_from_db()
    assert paid_stone.report == second


def test_a_stone_cannot_hold_two_live_reports(paid_stone, user):
    """Refused in the service, so the caller is told what to do instead."""
    create_report(stone=paid_stone, user=user)

    with pytest.raises(ServiceError, match="already has a report"):
        create_report(stone=paid_stone, user=user)


def test_a_stone_with_no_report_reads_as_none(paid_stone):
    """`Stone.report` stands in for the OneToOne accessor it replaced."""
    assert paid_stone.report is None


def test_a_discarded_report_is_not_findings(paid_stone, user):
    """A finalized report that was discarded does not certify its stone.

    The queues join to the report table, and a database join sees soft-deleted
    rows - the model's default manager does not reach into one. Without the
    `deleted_at` condition on that join, a discarded sign-off would still hold
    the stone out of the findings queue and push it into the certification one.
    """
    report = create_finalizable_report(paid_stone, user)
    finalize_report(report, user=user)
    assert paid_stone not in findings_worklist()

    report.delete()
    assert paid_stone in findings_worklist(), "discarded findings are not findings"
    assert paid_stone not in certification_worklist()


def test_restoring_is_refused_when_the_stone_was_reported_on_again(
    paid_stone, admin_user, auth_client
):
    """The newer report is the real record, so the older stays in the bin.

    Without this the restore would put two live reports on one stone, which the
    conditional constraint answers with an IntegrityError and a 500.
    """
    first = create_report(stone=paid_stone)
    first.delete()
    create_report(stone=paid_stone)

    response = auth_client(admin_user).post(
        f"/api/v1/identification-reports/{first.pk}/restore/"
    )

    assert response.status_code == 400
    assert "newer report" in str(response.data)
    first.refresh_from_db()
    assert first.is_deleted


def test_restoring_works_when_the_stone_is_still_free(
    paid_stone, admin_user, auth_client
):
    """Nothing took the slot, so the report comes back."""
    report = create_report(stone=paid_stone)
    report.delete()

    response = auth_client(admin_user).post(
        f"/api/v1/identification-reports/{report.pk}/restore/"
    )

    assert response.status_code == 200, response.data
    report.refresh_from_db()
    assert not report.is_deleted
    assert paid_stone.report == report


def test_the_findings_queue_costs_a_constant_number_of_queries(settings, user):
    """Two queries whatever the queue's length: the rows, then their reports.

    `Stone.report` reads a reverse FK, which is one query per row unless the
    queryset prefetches it - so the queue is exactly the kind of screen where
    that mistake would go unnoticed until the bench had a real backlog.
    """
    settings.GEPG_SIMULATE = True
    stone_type = StoneTypeFactory(price=Decimal("500.00"))
    for _ in range(6):
        order = OrderFactory(stone_count=1)
        stone = add_stone(order, stone_type=stone_type)
        simulate_payment(generate_bill_for_order(order))
        stone.refresh_from_db()
        create_report(stone=stone, user=user)

    with CaptureQueriesContext(connection) as queries:
        rows = StoneSerializer(list(findings_worklist()), many=True).data

    assert len(rows) == 6
    assert all(row["report_detail"] for row in rows)
    assert len(queries.captured_queries) == 2, [
        q["sql"] for q in queries.captured_queries
    ]


# ---------------------------------------------------------------------------
# Who may countersign a report
# ---------------------------------------------------------------------------


def test_the_bench_can_list_its_own_candidates(gemmologist_user, auth_client):
    """The dialog's list must work for the role that actually finalizes.

    Regression: this list used to come from ``/users``, which the gemmologist
    role holds no permission on. The 403 was swallowed by the client and the
    dropdown simply rendered empty, so a report could not be countersigned at
    all without anyone being told why.
    """
    response = auth_client(gemmologist_user).get(
        "/api/v1/identification-reports/gemmologist-candidates/"
    )

    assert response.status_code == 200


def test_candidates_are_gemmologists_other_than_the_caller(
    gemmologist_user, admin_user, viewer_user, auth_client
):
    """Only the bench, and never the caller themselves."""
    from django.contrib.auth.models import Group

    from apps.users.tests.factories import UserFactory

    peer = UserFactory()
    peer.groups.add(Group.objects.get(name="gemmologist"))

    response = auth_client(gemmologist_user).get(
        "/api/v1/identification-reports/gemmologist-candidates/"
    )

    returned = {row["id"] for row in response.data}
    assert peer.id in returned
    # Not the caller: `finalize_report` refuses a report signed twice over.
    assert gemmologist_user.id not in returned
    # Not other roles, however privileged - the certificate claims two
    # qualified gemmologists, so a manager or receptionist is not eligible.
    assert admin_user.id not in returned
    assert viewer_user.id not in returned


def test_an_inactive_gemmologist_is_not_offered(gemmologist_user, auth_client):
    """Someone who has left the lab cannot be named on a new certificate."""
    from django.contrib.auth.models import Group

    from apps.users.tests.factories import UserFactory

    retired = UserFactory(is_active=False)
    retired.groups.add(Group.objects.get(name="gemmologist"))

    response = auth_client(gemmologist_user).get(
        "/api/v1/identification-reports/gemmologist-candidates/"
    )

    assert retired.id not in {row["id"] for row in response.data}


def test_finalize_refuses_a_second_signatory_off_the_bench(
    paid_stone, gemmologist_user, viewer_user, auth_client
):
    """The rule lives on the server, not in the dialog.

    Narrowing the dropdown is a convenience; this is the check that keeps the
    certificate's "at least two qualified Gemmologists" true for a caller who
    posts the id by hand.
    """
    report = create_finalizable_report(paid_stone, gemmologist_user)

    response = auth_client(gemmologist_user).post(
        f"/api/v1/identification-reports/{report.id}/finalize/",
        {"verified_by": viewer_user.id},
        format="json",
    )

    assert response.status_code == 400
    report.refresh_from_db()
    assert not report.is_finalized
