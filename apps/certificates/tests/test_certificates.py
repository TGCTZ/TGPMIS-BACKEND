"""Certificate issuance, its guards, PDF download and verification."""

from decimal import Decimal

import pytest

from django.template.loader import render_to_string

from apps.billing.dev import simulate_payment
from apps.billing.services import generate_bill_for_order
from apps.certificates.models import Certificate
from apps.certificates.selectors import certification_worklist
from apps.certificates.services import assets, issue_certificate
from apps.certificates.services.pdf import TEMPLATE, certificate_context
from apps.core.exceptions import ServiceError
from apps.core.services import format_reference_number
from apps.gems.enums import (
    CertificateStatus,
    NatureType,
    OpticCharacter,
    StoneStatus,
    Transparency,
    WeightUnit,
)
from apps.gems.tests.factories import (
    ColorFactory,
    InstrumentFactory,
    OriginFactory,
    ShapeCutFactory,
    SpeciesFactory,
    StoneTypeFactory,
    TreatmentFactory,
    VarietyFactory,
)
from apps.identification.models import InstrumentUsed
from apps.identification.services import create_report, finalize_report
from apps.identification.tests.factories import create_finalizable_report
from apps.orders.models import StatusHistory, Stone
from apps.orders.services import add_stone, update_stone
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


def _paid_stone(settings, *, weight=Decimal("2.500")):
    """A stone whose order is billed and settled, and weighed at the bench.

    Weight arrives after the refresh, not with ``add_stone``: identification
    records the category only, and the bench records type and weight later.
    """
    settings.GEPG_SIMULATE = True
    order = OrderFactory(stone_count=1)
    stone = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("50000.00")))
    simulate_payment(generate_bill_for_order(order))
    stone.refresh_from_db()
    if weight is not None:
        update_stone(stone, weight=weight)
    return stone


@pytest.fixture
def certifiable_stone(settings, user):
    """A paid stone with a finalized report - ready to certify."""
    stone = _paid_stone(settings)
    report = create_finalizable_report(
        stone,
        user,
        color=ColorFactory(name="Red"),
        origin=OriginFactory(name="Tanzania"),
    )
    finalize_report(report, user=user)
    return stone


def test_issuing_freezes_the_findings(certifiable_stone, user):
    """The snapshot is the point: the document must keep saying the same thing."""
    certificate = issue_certificate(certifiable_stone, user=user)

    assert certificate.certificate_number.startswith("CERT-")
    assert certificate.certificate_number == certificate.report.report_number.replace(
        "TGC-", "CERT-", 1
    )
    assert certificate.stone_type_snapshot == certifiable_stone.stone_type.name
    assert certificate.weight_snapshot == Decimal("2.500")
    assert certificate.weight_unit_snapshot == WeightUnit.CARAT
    assert certificate.color_snapshot == "Red"
    assert certificate.origin_snapshot == "Tanzania"
    assert certificate.status == CertificateStatus.ISSUED


def test_snapshots_survive_the_lookup_being_renamed(certifiable_stone, user):
    """Renaming a colour must not rewrite a certificate already issued."""
    certificate = issue_certificate(certifiable_stone, user=user)
    color = certifiable_stone.report.color
    color.name = "Crimson"
    color.save(update_fields=["name"])

    certificate.refresh_from_db()
    assert certificate.color_snapshot == "Red"


def test_issuing_certifies_the_stone(certifiable_stone, user):
    """Certification is the stone's last status move, and it is recorded."""
    certificate = issue_certificate(certifiable_stone, user=user)

    certifiable_stone.refresh_from_db()
    assert certifiable_stone.status == StoneStatus.CERTIFIED

    entry = StatusHistory.objects.filter(
        stone=certifiable_stone, to_status=StoneStatus.CERTIFIED
    ).latest("changed_at")
    assert format_reference_number(certificate.certificate_number) in entry.note


def test_a_stone_cannot_be_certified_twice(certifiable_stone, user):
    """One certificate per stone."""
    issue_certificate(certifiable_stone, user=user)

    with pytest.raises(ServiceError, match="already has a certificate"):
        issue_certificate(certifiable_stone, user=user)


def test_certification_needs_a_finalized_report(settings, user):
    """A draft is not evidence."""
    stone = _paid_stone(settings)
    create_report(stone=stone, user=user)

    with pytest.raises(ServiceError, match="no finalized identification report"):
        issue_certificate(stone, user=user)


def test_certification_needs_a_report_at_all(settings, user):
    """Nothing to certify without findings."""
    stone = _paid_stone(settings)

    with pytest.raises(ServiceError, match="no finalized identification report"):
        issue_certificate(stone, user=user)


def test_certification_needs_a_settled_bill(certifiable_stone, user):
    """Payment gates the document, not just the bench work.

    The bill is un-settled directly rather than by building an unpaid stone,
    because the guards run in order: a stone with no finalized report is refused
    on the report guard and never reaches this one. And a report cannot be
    created before payment in the first place - so this guard is defence in
    depth against a bill that is later reversed.
    """
    from apps.gems.enums import BillStatus

    bill = certifiable_stone.order.bill
    bill.status = BillStatus.PARTIALLY_PAID
    bill.save(update_fields=["status"])

    with pytest.raises(ServiceError, match="bill must be fully paid"):
        issue_certificate(certifiable_stone, user=user)


def test_certification_needs_a_recorded_weight(settings, user):
    """A certificate states a weight, so it must have one.

    Defence in depth rather than a state the workflow can reach on its own:
    ``finalize_report`` now insists on a weight, so the stone is stripped of one
    *after* sign-off to reach this guard. Without it the missing weight would
    surface as an IntegrityError on the non-null snapshot column rather than as
    the rule it actually expresses.
    """
    stone = _paid_stone(settings, weight=None)
    finalize_report(create_finalizable_report(stone, user), user=user)
    stone.refresh_from_db()
    Stone.objects.filter(pk=stone.pk).update(weight=None)
    stone.refresh_from_db()

    with pytest.raises(ServiceError, match="no recorded weight"):
        issue_certificate(stone, user=user)


def test_certification_requires_a_photograph(settings, user):
    """Legacy finalized reports still cannot issue without a photograph."""
    stone = _paid_stone(settings)
    report = create_finalizable_report(stone, user)
    finalize_report(report, user=user)
    stone.photo.delete(save=True)

    with pytest.raises(ServiceError, match="no photograph"):
        issue_certificate(stone, user=user)


def test_certification_worklist_is_the_three_guards_as_a_queue(
    settings, certifiable_stone, user
):
    """A stone enters when its report is signed off, and leaves when certified."""
    unfinalized = _paid_stone(settings)
    create_report(stone=unfinalized, user=user)

    assert certifiable_stone in certification_worklist()
    assert unfinalized not in certification_worklist()

    issue_certificate(certifiable_stone, user=user)
    assert certifiable_stone not in certification_worklist()


def test_issue_endpoint_requires_the_issue_permission(
    certifiable_stone, viewer_user, auth_client
):
    """Reception hands the certificate over; it does not issue it."""
    response = auth_client(viewer_user).post(
        "/api/v1/certificates/", {"stone": certifiable_stone.pk}
    )

    assert response.status_code == 403
    assert not Certificate.objects.filter(stone=certifiable_stone).exists()


def test_issue_endpoint_creates_the_certificate(
    certifiable_stone, admin_user, auth_client
):
    """The endpoint delegates wholly to the service."""
    response = auth_client(admin_user).post(
        "/api/v1/certificates/", {"stone": certifiable_stone.pk}
    )

    assert response.status_code == 201, response.data
    assert response.data["certificate_number"].startswith("CERT-")
    assert response.data["stone_label"] == certifiable_stone.label


def test_certificate_fields_are_not_client_writable(
    certifiable_stone, admin_user, auth_client
):
    """The snapshots are the document; a client must not edit them."""
    certificate = issue_certificate(certifiable_stone)

    response = auth_client(admin_user).patch(
        f"/api/v1/certificates/{certificate.pk}/",
        {"stone_type_snapshot": "Something else", "status": CertificateStatus.REVOKED},
    )

    assert response.status_code == 200, response.data
    certificate.refresh_from_db()
    assert certificate.stone_type_snapshot != "Something else"
    assert certificate.status == CertificateStatus.ISSUED


def test_pdf_download_returns_a_pdf(certifiable_stone, admin_user, auth_client):
    """The PDF is how a certificate leaves the system."""
    certificate = issue_certificate(certifiable_stone)

    response = auth_client(admin_user).get(f"/api/v1/certificates/{certificate.pk}/pdf/")

    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert certificate.certificate_number in response["Content-Disposition"]


def test_pdf_download_requires_the_view_permission(
    certifiable_stone, viewer_user, auth_client
):
    """Reception holds no certificate permissions, so it cannot pull documents."""
    certificate = issue_certificate(certifiable_stone)

    response = auth_client(viewer_user).get(f"/api/v1/certificates/{certificate.pk}/pdf/")

    assert response.status_code == 403


def test_a_revoked_certificate_still_downloads(
    certifiable_stone, admin_user, auth_client
):
    """Legacy revoked records retain their historical PDF watermark."""
    certificate = issue_certificate(certifiable_stone)
    certificate.status = CertificateStatus.REVOKED
    certificate.save(update_fields=["status"])

    response = auth_client(admin_user).get(f"/api/v1/certificates/{certificate.pk}/pdf/")

    assert response.status_code == 200
    assert "-revoked.pdf" in response["Content-Disposition"]


def test_the_document_says_what_it_said_when_issued(certifiable_stone, user):
    """Renaming a colour afterwards must not rewrite a document already handed over.

    Asserted against the template context rather than the PDF bytes: the
    guarantee is about which values reach the page, and parsing a PDF to prove
    it would test WeasyPrint instead.
    """
    certificate = issue_certificate(certifiable_stone, user=user)
    report = certificate.report
    report.color.name = "Crimson"
    report.color.save()

    context = certificate_context(certificate)

    assert context["certificate"].color_snapshot == "Red"
    assert context["order_reference"] == format_reference_number(
        certifiable_stone.order.reference_number
    )
    assert context["customer_name"] == certifiable_stone.order.customer.full_name
    assert context["is_revoked"] is False


# ---------------------------------------------------------------------------
# The full snapshot set, the QR, and public verification
# ---------------------------------------------------------------------------
def test_certificate_freezes_every_finding_it_prints(settings, user):
    """The document keeps its own copy of each word, not a live reference.

    The point of the whole snapshot block: editing the lookup rows afterwards
    must not change what an issued certificate says.
    """
    stone = _paid_stone(settings)
    species = SpeciesFactory(name="Corundum")
    variety = VarietyFactory(name="Ruby")
    shape = ShapeCutFactory(name="Oval")
    report = create_finalizable_report(
        stone,
        user,
        species=species,
        variety=variety,
        shape_cut=shape,
        color=ColorFactory(name="Pigeon blood"),
        origin=OriginFactory(name="Mogok"),
        transparency=Transparency.TRANSPARENT,
        optic_character=OpticCharacter.DR,
        treatment=TreatmentFactory(name="Heated"),
        nature_type=NatureType.NATURAL,
        refractive_index="1.762-1.770",
        conclusion="Natural ruby, heated.",
    )
    finalize_report(report, user=user)

    certificate = issue_certificate(stone, user=user)

    assert certificate.species_snapshot == "Corundum"
    assert certificate.variety_snapshot == "Ruby"
    assert certificate.shape_cut_snapshot == "Oval"
    assert certificate.origin_snapshot == "Mogok"
    assert certificate.refractive_index_snapshot == "1.762-1.770"
    assert certificate.comments_snapshot == "Natural ruby, heated."
    assert certificate.report_number_snapshot == report.report_number

    # Enum labels, not stored codes - the document is read by a customer.
    assert certificate.transparency_snapshot == "Transparent"
    assert certificate.treatment_snapshot == "Heated"
    assert "refractive" in certificate.optic_character_snapshot.lower()

    # Now rewrite the world the findings came from.
    species.name = "Renamed later"
    species.save()
    variety.name = "Also renamed"
    variety.save()

    certificate.refresh_from_db()
    assert certificate.species_snapshot == "Corundum"
    assert certificate.variety_snapshot == "Ruby"


def test_certificate_names_both_gemmologists(settings, user, admin_user):
    """Two signatories, because the document claims two examined the stone."""
    stone = _paid_stone(settings)
    report = create_finalizable_report(stone, user)
    finalize_report(report, user=user, verified_by=admin_user)

    certificate = issue_certificate(stone, user=user)

    assert certificate.gemmologist == (user.get_full_name() or user.username)
    assert certificate.gemmologist_two == (
        admin_user.get_full_name() or admin_user.username
    )


def test_the_second_gemmologist_must_be_someone_else(settings, user):
    """A second opinion from the same head is not a second opinion."""
    stone = _paid_stone(settings)
    report = create_finalizable_report(stone, user)

    with pytest.raises(ServiceError, match="different person"):
        finalize_report(report, user=user, verified_by=user)


def test_certificate_snapshots_the_instrument_checklist(settings, user):
    """Every active instrument is listed, ticked when used, frozen as words."""
    stone = _paid_stone(settings)
    report = create_finalizable_report(stone, user)
    used = InstrumentFactory(name="Refractometer")
    InstrumentFactory(name="Polariscope")
    retired = InstrumentFactory(name="Dichroscope", is_active=False)
    InstrumentUsed.objects.create(report=report, instrument=used)
    finalize_report(report, user=user)

    certificate = issue_certificate(stone, user=user)

    snapshot = {row["name"]: row["used"] for row in certificate.instruments_snapshot}
    assert snapshot["Refractometer"] is True
    assert snapshot["Polariscope"] is False
    assert retired.name not in snapshot

    used.name = "Renamed instrument"
    used.save()
    certificate.refresh_from_db()
    assert "Refractometer" in {row["name"] for row in certificate.instruments_snapshot}


def test_legacy_instrument_snapshot_renders_as_ticked(settings, user):
    """Certificates issued before the checklist listed only instruments used."""
    stone = _paid_stone(settings)
    report = create_finalizable_report(stone, user)
    finalize_report(report, user=user)
    certificate = issue_certificate(stone, user=user)
    certificate.instruments_snapshot = [{"name": "Refractometer", "reading": "1.76"}]

    InstrumentFactory(name="Polariscope")

    instruments = {
        row["name"]: row["used"]
        for row in certificate_context(certificate)["instruments"]
    }
    assert instruments["Refractometer"] is True
    assert instruments["Polariscope"] is False


def test_certificate_context_carries_a_qr_and_the_lab_marks(settings, user):
    """The QR is built at render time and points at the public verify URL."""
    settings.CERTIFICATE_VERIFY_BASE_URL = "https://tgc.example"
    stone = _paid_stone(settings)
    report = create_finalizable_report(stone, user)
    finalize_report(report, user=user)
    certificate = issue_certificate(stone, user=user)

    context = certificate_context(certificate)

    assert context["verify_url"] == (
        f"https://tgc.example/verify/{certificate.certificate_number}/"
    )
    assert context["qr_code"].startswith("data:image/png;base64,")
    # Assets are optional: a lab that has not supplied its stamp still gets a
    # certificate, so the key is present and may be None.
    assert "official_stamp" in context


def test_verify_page_is_public_and_reports_a_valid_certificate(certifiable_stone, client):
    """Whoever holds the paper can check it without an account."""
    stone = certifiable_stone
    certificate = issue_certificate(stone)

    response = client.get(f"/verify/{certificate.certificate_number}/")

    assert response.status_code == 200
    body = response.content.decode()
    assert "Valid certificate" in body
    assert format_reference_number(certificate.certificate_number) in body
    assert 'alt="National emblem of Tanzania"' in body
    assert 'alt="Tanzania Gemmological Centre logo"' in body
    assert "<h2>Gemstone Photo</h2>" in body
    assert body.index("<h2>Gemstone Photo</h2>") < body.index("<h2>Description</h2>")
    assert "Colour" in body
    assert "Shape / Cut" in body
    assert "Optic character" in body
    assert "Specific gravity" in body
    assert "Group / Species" in body
    assert "Treatment" in body
    assert "Gemmologist 1" in body
    assert "Official Stamp" not in body
    assert "Instruments Used" not in body
    # The public page identifies the stone, never its owner.
    assert stone.order.customer.full_name not in body


def test_verify_page_says_so_when_a_certificate_is_revoked(settings, user, client):
    """The public verifier continues to identify legacy revoked records."""
    stone = _paid_stone(settings)
    report = create_finalizable_report(stone, user)
    finalize_report(report, user=user)
    certificate = issue_certificate(stone, user=user)
    certificate.status = CertificateStatus.REVOKED
    certificate.save(update_fields=["status"])

    response = client.get(f"/verify/{certificate.certificate_number}/")

    assert response.status_code == 200
    assert "no longer stands" in response.content.decode()


def test_verify_page_answers_plainly_for_an_unknown_number(client):
    """A 404 page would tell whoever scanned it nothing useful."""
    response = client.get("/verify/CERT-2026-9999/")

    assert response.status_code == 404
    assert "Certificate not found" in response.content.decode()


# ---------------------------------------------------------------------------
# Lab marks


def test_a_mark_supplied_later_appears_without_a_restart(request, monkeypatch, tmp_path):
    """Dropping a mark into the asset directory must not need a process restart.

    A loader that cached the *miss* as readily as the hit would keep printing
    the empty box after the stamp is supplied, until a restart - and nothing
    about a new PNG makes Django's autoreloader restart one.
    """
    # Both ends: the loader must not start with a real mark already encoded,
    # and must not leave this temporary one behind for the next test.
    request.addfinalizer(assets.forget_assets)
    monkeypatch.setattr(assets, "ASSET_DIR", tmp_path)
    assets.forget_assets()

    assert assets.asset_data_uri("official_stamp") is None

    (tmp_path / assets.ASSETS["official_stamp"]).write_bytes(b"not really a png")

    assert assets.asset_data_uri("official_stamp").startswith("data:image/png;base64,")


def test_default_header_shows_both_marks_and_all_titles(settings, certifiable_stone):
    """The clean default keeps the original masthead details as separate elements."""
    certificate = issue_certificate(certifiable_stone)

    context = certificate_context(certificate)
    html = render_to_string(TEMPLATE, context)

    assert context["header_style"] == "clean"
    assert context["coat_of_arms"].startswith("data:image/png;base64,")
    assert context["tgc_logo"].startswith("data:image/png;base64,")
    assert "United Republic of Tanzania" in html
    assert "The United Republic of Tanzania" not in html
    assert settings.CERTIFICATE_MINISTRY_NAME in html
    assert settings.CERTIFICATE_LAB_NAME in html
    assert "Gemstone Identification Report" in html


def test_the_document_carries_its_own_typefaces(certifiable_stone):
    """Every face the certificate names must also be embedded in it.

    The failure this guards against is silent rather than loud: the render host
    has neither family installed, so a font file that goes missing or gets
    renamed does not raise - the document simply renders in whatever the system
    substitutes, and the lab issues a subtly different certificate indefinitely.
    """
    certificate = issue_certificate(certifiable_stone)

    faces = certificate_context(certificate)["font_faces"]

    assert faces.count("@font-face") == len(assets.FONTS)
    # Embedded, not merely referenced: a src pointing anywhere but at inline
    # bytes would mean WeasyPrint has to resolve something at render time.
    assert faces.count("src:url(data:font/woff2;base64,") == len(assets.FONTS)


def test_no_template_syntax_leaks_onto_the_document(certifiable_stone):
    """The rendered certificate must contain no unrendered template syntax.

    Django's ``{# ... #}`` is a *single-line* comment. Spread one over two
    lines and the remainder is not a comment at all - it is content, and it
    prints on the certificate. It is an easy slip when editing the template and
    invisible to a test that only reads the context, so it is asserted against
    the rendered HTML here.
    """
    certificate = issue_certificate(certifiable_stone)

    html = render_to_string(TEMPLATE, certificate_context(certificate))

    for token in ("{#", "#}", "{%", "%}", "{{", "}}"):
        assert token not in html, f"Unrendered {token!r} reached the document."
