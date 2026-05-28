"""
ESG Data Ingestion & Normalization Models
Multi-tenant, audit-ready, GHG Protocol aligned.
"""

import uuid
from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError


class Organization(models.Model):
    """Tenant model — every piece of data is scoped to one org."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "organization"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SourceSystem(models.Model):
    """Tracks the configuration of each incoming data stream."""

    class SourceType(models.TextChoices):
        SAP_EXPORT = "SAP_EXPORT", "SAP Export"
        UTILITY_CSV = "UTILITY_CSV", "Utility Portal CSV"
        CONCUR_JSON = "CONCUR_JSON", "Corporate Travel JSON"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    source_type = models.CharField(max_length=50, choices=SourceType.choices)
    tenant = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="source_systems"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "source_system"
        unique_together = [("tenant", "name")]

    def __str__(self):
        return f"{self.name} ({self.source_type})"


class RawIngestionLog(models.Model):
    """Immutable audit record for every uploaded file or payload."""

    class ProcessedStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial Success"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="ingestion_logs"
    )
    source_system = models.ForeignKey(
        SourceSystem, on_delete=models.SET_NULL, null=True, related_name="ingestion_logs"
    )
    filename = models.CharField(max_length=512, blank=True, default="")
    # JSONB snapshot of the raw payload for full lineage tracing
    raw_payload_snapshot = models.JSONField(default=dict)
    processed_status = models.CharField(
        max_length=20,
        choices=ProcessedStatus.choices,
        default=ProcessedStatus.PENDING,
    )
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "raw_ingestion_log"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.filename or 'payload'} [{self.processed_status}] @ {self.created_at:%Y-%m-%d %H:%M}"


class NormalizedEmissionActivity(models.Model):
    """
    Unified, audit-ready emission record.
    Every source normalizes into this shape.
    Approved rows become immutable.
    """

    class ScopeCategory(models.TextChoices):
        SCOPE_1 = "SCOPE_1", "Scope 1 — Direct Emissions"
        SCOPE_2 = "SCOPE_2", "Scope 2 — Indirect (Electricity)"
        SCOPE_3 = "SCOPE_3", "Scope 3 — Value Chain"

    class ActivityType(models.TextChoices):
        FUEL_COMBUSTION = "FUEL_COMBUSTION", "Fuel Combustion"
        ELECTRICITY_CONSUMPTION = "ELECTRICITY_CONSUMPTION", "Electricity Consumption"
        BUSINESS_TRAVEL = "BUSINESS_TRAVEL", "Business Travel"

    class RecordStatus(models.TextChoices):
        PENDING_REVIEW = "PENDING_REVIEW", "Pending Review"
        FLAGGED_ANOMALY = "FLAGGED_ANOMALY", "Flagged Anomaly"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="emission_activities"
    )
    raw_log = models.ForeignKey(
        RawIngestionLog,
        on_delete=models.PROTECT,
        related_name="normalized_records",
        help_text="Lineage pointer back to the raw ingestion event.",
    )
    scope_category = models.CharField(max_length=20, choices=ScopeCategory.choices)
    activity_type = models.CharField(max_length=30, choices=ActivityType.choices)

    # Temporal coverage — critical for billing-cycle vs calendar-month alignment
    start_date = models.DateField()
    end_date = models.DateField()

    # Source values preserved verbatim for auditability
    original_value = models.DecimalField(max_digits=18, decimal_places=4)
    original_unit = models.CharField(max_length=20)

    # Normalized to a canonical unit (liters for fuel, kWh for electricity,
    # passenger-km for travel)
    normalized_value_liters_or_kwh = models.FloatField()

    # Calculated CO₂e using hardcoded emission factors (kg CO₂e)
    calculated_co2e_kg = models.FloatField()

    facility_code = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="WERKS / plant code from source system.",
    )

    status = models.CharField(
        max_length=20,
        choices=RecordStatus.choices,
        default=RecordStatus.PENDING_REVIEW,
    )
    anomaly_reason = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "normalized_emission_activity"
        ordering = ["-start_date"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "scope_category"]),
            models.Index(fields=["facility_code"]),
        ]

    def clean(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError("start_date must be before end_date.")

    def save(self, *args, **kwargs):
        # Approved records are immutable — block any further saves
        if self.pk:
            try:
                existing = NormalizedEmissionActivity.objects.get(pk=self.pk)
                if existing.status == self.RecordStatus.APPROVED:
                    raise ValidationError(
                        "Approved emission records are immutable and cannot be modified."
                    )
            except NormalizedEmissionActivity.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.scope_category} | {self.activity_type} | "
            f"{self.start_date} → {self.end_date} | {self.calculated_co2e_kg:.2f} kg CO₂e"
        )


class AuditLog(models.Model):
    """
    Append-only log of every human modification to a NormalizedEmissionActivity.
    Never delete rows from this table.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    activity_record = models.ForeignKey(
        NormalizedEmissionActivity,
        on_delete=models.PROTECT,
        related_name="audit_logs",
    )
    changed_by_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="esg_audit_logs",
    )
    field_name = models.CharField(max_length=100)
    old_value = models.TextField(blank=True, null=True)
    new_value = models.TextField(blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    reason_for_change = models.TextField()

    class Meta:
        db_table = "audit_log"
        ordering = ["-timestamp"]

    def __str__(self):
        return (
            f"AuditLog: {self.activity_record_id} | "
            f"{self.field_name}: {self.old_value!r} → {self.new_value!r} "
            f"by {self.changed_by_user} @ {self.timestamp:%Y-%m-%d %H:%M}"
        )
