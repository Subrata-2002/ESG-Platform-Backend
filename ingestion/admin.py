"""Admin registrations for the ESG ingestion models."""

from django.contrib import admin

from .models import (
    AuditLog,
    NormalizedEmissionActivity,
    Organization,
    RawIngestionLog,
    SourceSystem,
)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["name", "id", "created_at"]
    search_fields = ["name"]


@admin.register(SourceSystem)
class SourceSystemAdmin(admin.ModelAdmin):
    list_display = ["name", "source_type", "tenant", "created_at"]
    list_filter = ["source_type", "tenant"]


@admin.register(RawIngestionLog)
class RawIngestionLogAdmin(admin.ModelAdmin):
    list_display = ["filename", "tenant", "source_system", "processed_status", "created_at"]
    list_filter = ["processed_status", "tenant"]
    readonly_fields = ["raw_payload_snapshot"]


@admin.register(NormalizedEmissionActivity)
class NormalizedEmissionActivityAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "tenant",
        "scope_category",
        "activity_type",
        "start_date",
        "end_date",
        "calculated_co2e_kg",
        "status",
    ]
    list_filter = ["status", "scope_category", "activity_type", "tenant"]
    search_fields = ["facility_code"]
    readonly_fields = ["id", "raw_log", "created_at", "updated_at"]

    def has_change_permission(self, request, obj=None):
        # Block admin edits on approved records
        if obj and obj.status == NormalizedEmissionActivity.RecordStatus.APPROVED:
            return False
        return super().has_change_permission(request, obj)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["activity_record", "field_name", "old_value", "new_value", "changed_by_user", "timestamp"]
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
