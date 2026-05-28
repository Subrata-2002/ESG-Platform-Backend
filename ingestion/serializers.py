"""
DRF Serializers for the ESG ingestion platform.
"""

from rest_framework import serializers

from .models import AuditLog, NormalizedEmissionActivity, RawIngestionLog


class RawIngestionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = RawIngestionLog
        fields = [
            "id",
            "tenant",
            "source_system",
            "filename",
            "processed_status",
            "error_message",
            "created_at",
        ]
        read_only_fields = fields


class NormalizedEmissionActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = NormalizedEmissionActivity
        fields = [
            "id",
            "tenant",
            "raw_log",
            "scope_category",
            "activity_type",
            "start_date",
            "end_date",
            "original_value",
            "original_unit",
            "normalized_value_liters_or_kwh",
            "calculated_co2e_kg",
            "facility_code",
            "status",
            "anomaly_reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "tenant",
            "raw_log",
            "scope_category",
            "activity_type",
            "normalized_value_liters_or_kwh",
            "calculated_co2e_kg",
            "status",
            "anomaly_reason",
            "created_at",
            "updated_at",
        ]


class AuditLogSerializer(serializers.ModelSerializer):
    changed_by_username = serializers.CharField(
        source="changed_by_user.username", read_only=True, default=None
    )

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "activity_record",
            "changed_by_username",
            "field_name",
            "old_value",
            "new_value",
            "timestamp",
            "reason_for_change",
        ]
        read_only_fields = fields


class IngestRequestSerializer(serializers.Serializer):
    """Validates the multipart/form-data or JSON body for POST /api/ingest/."""

    SOURCE_TYPE_CHOICES = [
        ("SAP_EXPORT", "SAP Export"),
        ("UTILITY_CSV", "Utility Portal CSV"),
        ("CONCUR_JSON", "Corporate Travel JSON"),
    ]

    source_type = serializers.ChoiceField(choices=SOURCE_TYPE_CHOICES)
    tenant_id = serializers.UUIDField()
    # Either a file upload or a raw text/JSON payload
    file = serializers.FileField(required=False)
    raw_payload = serializers.CharField(required=False, allow_blank=False)

    def validate(self, attrs):
        if not attrs.get("file") and not attrs.get("raw_payload"):
            raise serializers.ValidationError(
                "Provide either a 'file' upload or a 'raw_payload' string."
            )
        return attrs


class EditRecordSerializer(serializers.Serializer):
    """Validates PATCH /api/records/<id>/edit/ body."""

    original_value = serializers.DecimalField(max_digits=18, decimal_places=4)
    original_unit = serializers.CharField(max_length=20)
    reason_for_change = serializers.CharField(min_length=5)


class RejectRecordSerializer(serializers.Serializer):
    """Validates POST /api/records/<id>/reject/ body."""

    reason_for_change = serializers.CharField(min_length=5)
