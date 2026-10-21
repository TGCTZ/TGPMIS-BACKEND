"""Validation and response contracts for report requests."""

from rest_framework import serializers

from django.utils import timezone

from apps.gems.enums import BillStatus

from .definitions import REPORT_TIMEZONE, SECTIONS


class ReportQuerySerializer(serializers.Serializer):
    """One inclusive lab-day range shared by a report's sections."""

    from_date = serializers.DateField(required=False)
    to_date = serializers.DateField(required=False)
    section = serializers.ChoiceField(choices=tuple(SECTIONS), required=False)
    customer = serializers.IntegerField(min_value=1, required=False)
    status = serializers.ChoiceField(choices=BillStatus.choices, required=False)
    stone_type = serializers.IntegerField(min_value=1, required=False)
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=15)
    file_type = serializers.ChoiceField(choices=("xlsx", "pdf"), default="xlsx")

    def validate(self, attrs):
        """Default to this month and reject reversed ranges rather than guessing."""
        today = timezone.localdate(timezone=REPORT_TIMEZONE)
        attrs.setdefault("from_date", today.replace(day=1))
        attrs.setdefault("to_date", today)
        if attrs["from_date"] > attrs["to_date"]:
            raise serializers.ValidationError(
                "The start date must be on or before the end."
            )
        return attrs


class ReportSectionSerializer(serializers.Serializer):
    """A section's full-result totals, independent of its detail page."""

    key = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    count = serializers.IntegerField()
    missing_dates = serializers.IntegerField()
    amounts = serializers.ListField(child=serializers.DictField())
    statuses = serializers.ListField(child=serializers.DictField())
    categories = serializers.ListField(child=serializers.DictField())
    varieties = serializers.ListField(child=serializers.DictField())


class ReportResultSerializer(serializers.Serializer):
    """The standard list envelope plus the report's summaries and filters."""

    report = serializers.CharField()
    title = serializers.CharField()
    range = serializers.DictField()
    generated_at = serializers.DateTimeField()
    sections = ReportSectionSerializer(many=True)
    section = serializers.CharField()
    columns = serializers.ListField(child=serializers.DictField())
    filters = serializers.DictField()
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = serializers.ListField(child=serializers.DictField())


class ReportCatalogSerializer(serializers.Serializer):
    """Only pages and sections available to the requesting user."""

    key = serializers.CharField()
    title = serializers.CharField()
    sections = serializers.ListField(child=serializers.DictField())
