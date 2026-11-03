"""Serializers for the identification domain."""

from rest_framework import serializers

from apps.core.serializers import AuditFieldsMixin, DisplayReferenceField
from apps.core.services import format_reference_number, reference_number_for_order
from apps.gems.enums import WeightUnit
from apps.gems.models import StoneType
from apps.gems.serializers import (
    ColorSerializer,
    InstrumentSerializer,
    OriginSerializer,
    ShapeCutSerializer,
    SpeciesSerializer,
    StoneCategorySerializer,
    StoneTypeSerializer,
    TreatmentSerializer,
    VarietySerializer,
)

from .models import IdentificationReport, InstrumentUsed
from .selectors import gemmologist_candidates


class InstrumentUsedSerializer(AuditFieldsMixin):
    """An instrument used during a report."""

    instrument_detail = InstrumentSerializer(source="instrument", read_only=True)

    class Meta:
        model = InstrumentUsed
        fields = (
            "id",
            "report",
            "instrument",
            "instrument_detail",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        read_only_fields = AuditFieldsMixin.AUDIT_FIELDS


class IdentificationReportSerializer(AuditFieldsMixin):
    """A gemmologist's findings for one stone."""

    species_detail = SpeciesSerializer(source="species", read_only=True)
    variety_detail = VarietySerializer(source="variety", read_only=True)
    origin_detail = OriginSerializer(source="origin", read_only=True)
    shape_cut_detail = ShapeCutSerializer(source="shape_cut", read_only=True)
    color_detail = ColorSerializer(source="color", read_only=True)
    treatment_detail = TreatmentSerializer(source="treatment", read_only=True)
    stone_category_detail = StoneCategorySerializer(
        source="stone.stone_category", read_only=True
    )
    stone_type = serializers.PrimaryKeyRelatedField(
        queryset=StoneType.objects.all(),
        required=False,
        allow_null=True,
        write_only=True,
    )
    stone_type_detail = StoneTypeSerializer(source="stone.stone_type", read_only=True)
    instruments_used = InstrumentUsedSerializer(many=True, read_only=True)

    stone_label = serializers.CharField(source="stone.label", read_only=True)
    report_number = DisplayReferenceField(read_only=True)
    order_reference = DisplayReferenceField(
        source="stone.order.reference_number", read_only=True
    )
    stone_reference = serializers.SerializerMethodField()
    # The customer, alongside the order they came in on. A reference number
    # alone identifies the paperwork; the name is what identifies the visit to
    # anyone reading a list of them.
    customer_name = serializers.CharField(
        source="stone.order.customer.full_name", read_only=True
    )
    customer_phone = serializers.CharField(
        source="stone.order.customer.phone", read_only=True
    )
    identified_by_label = serializers.SerializerMethodField()
    verified_by_label = serializers.SerializerMethodField()

    # Weight is measured at the bench alongside the other findings, so it belongs on
    # this form - but it lives on the Stone, which is what the certificate
    # snapshots. It travels through here and the service hands it on. Read back
    # under stone_* so the form seeds itself in one request.
    stone_weight = serializers.DecimalField(
        source="stone.weight", max_digits=10, decimal_places=3, read_only=True
    )
    stone_weight_unit = serializers.CharField(source="stone.weight_unit", read_only=True)
    weight = serializers.DecimalField(
        max_digits=10,
        decimal_places=3,
        required=False,
        allow_null=True,
        write_only=True,
    )
    # No default, deliberately: a default lands in validated_data on every
    # request, so a PATCH of the conclusion alone would write to the stone.
    weight_unit = serializers.ChoiceField(
        choices=WeightUnit.choices, required=False, write_only=True
    )

    class Meta:
        model = IdentificationReport
        fields = (
            "id",
            "stone",
            "stone_label",
            "order_reference",
            "stone_reference",
            "customer_name",
            "customer_phone",
            "stone_category_detail",
            "stone_type",
            "stone_type_detail",
            "report_number",
            "species",
            "species_detail",
            "variety",
            "variety_detail",
            "origin",
            "origin_detail",
            "shape_cut",
            "shape_cut_detail",
            "color",
            "color_detail",
            "nature_type",
            "transparency",
            "treatment",
            "treatment_detail",
            "optic_character",
            "refractive_index",
            "specific_gravity",
            "weight",
            "weight_unit",
            "stone_weight",
            "stone_weight_unit",
            "is_polished",
            "conclusion",
            "instruments_used",
            "is_finalized",
            "identified_by",
            "identified_by_label",
            "verified_by",
            "verified_by_label",
            "identified_at",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        # The workflow fields move only through the finalize action, and the
        # report number is allocated by the service.
        read_only_fields = (
            *AuditFieldsMixin.AUDIT_FIELDS,
            "report_number",
            "is_finalized",
            "identified_by",
            "verified_by",
            "identified_at",
        )

    def get_identified_by_label(self, obj) -> str | None:
        """The gemmologist's display name, or None if unattributed."""
        return str(obj.identified_by) if obj.identified_by_id else None

    def get_verified_by_label(self, obj) -> str | None:
        """The second gemmologist's display name, or None if only one signed."""
        return str(obj.verified_by) if obj.verified_by_id else None

    def get_stone_reference(self, report) -> str:
        """Identify the report's stone with the order sequence and label."""
        return format_reference_number(
            reference_number_for_order(
                report.stone.order, "ORD", stone_label=report.stone.label
            )
        )

    def validate(self, attrs):
        """Keep selected type/category and variety/species relationships valid."""
        stone_type = attrs.get("stone_type")
        stone = attrs.get("stone", self.instance.stone if self.instance else None)
        if (
            stone_type is not None
            and stone is not None
            and stone_type.category_id != stone.stone_category_id
        ):
            raise serializers.ValidationError(
                {"stone_type": "Choose a type belonging to this stone's category."}
            )

        species = attrs.get("species", getattr(self.instance, "species", None))
        variety = attrs.get("variety", getattr(self.instance, "variety", None))
        if variety and species and variety.species_id != species.pk:
            raise serializers.ValidationError(
                {"variety": "Choose a variety belonging to the selected species."}
            )
        if variety and species is None:
            raise serializers.ValidationError(
                {"species": "Select a species before selecting a variety."}
            )
        return attrs


class GemmologistCandidateSerializer(serializers.Serializer):
    """One person the caller may name as second gemmologist.

    Deliberately not the full user serializer: the dialog needs a name to show
    and an id to post back, and the bench has no business reading everyone's
    email address to fill in a dropdown.
    """

    id = serializers.IntegerField(read_only=True)
    label = serializers.SerializerMethodField()

    def get_label(self, obj) -> str:
        """The same rendering as ``verified_by_label`` on the report.

        Shared so the name in the dropdown is the name that comes back on the
        finalized report, rather than two spellings of the same person.
        """
        return str(obj)


class FinalizeReportSerializer(serializers.Serializer):
    """Payload for finalizing a report.

    ``verified_by`` identifies the required second gemmologist.
    """

    # Narrowed to the bench rather than every user: this field is what puts a
    # second name on a certificate that claims two qualified gemmologists saw
    # the stone, so the check belongs on the server, not in the dialog.
    verified_by = serializers.PrimaryKeyRelatedField(
        queryset=gemmologist_candidates(), required=True, allow_null=False
    )
