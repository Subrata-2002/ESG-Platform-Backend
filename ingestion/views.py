"""
DRF Views for the ESG ingestion platform.

Endpoints:
  POST   /api/ingest/                   — Upload / submit raw data
  GET    /api/dashboard/records/        — List normalized records (filterable)
  POST   /api/records/<id>/approve/     — Approve a record (makes it immutable)
  POST   /api/records/<id>/reject/      — Reject a record
  PATCH  /api/records/<id>/edit/        — Correct original_value (writes AuditLog)
"""

import json
import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AuditLog,
    NormalizedEmissionActivity,
    Organization,
    RawIngestionLog,
    SourceSystem,
)
from .serializers import (
    EditRecordSerializer,
    IngestRequestSerializer,
    NormalizedEmissionActivitySerializer,
    RejectRecordSerializer,
)
from .services import CorporateTravelParser, SAPExportParser, UtilityCSVParser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_source_system(
    tenant: Organization, source_type: str
) -> SourceSystem:
    """Lazily create a SourceSystem record for the given tenant + type."""
    obj, _ = SourceSystem.objects.get_or_create(
        tenant=tenant,
        source_type=source_type,
        defaults={"name": f"{source_type} (auto-created)"},
    )
    return obj


# ---------------------------------------------------------------------------
# POST /api/ingest/
# ---------------------------------------------------------------------------

class IngestView(APIView):
    """
    Accepts file uploads or raw payloads and routes them to the correct parser.

    Multipart form fields:
      source_type  — 'SAP_EXPORT' | 'UTILITY_CSV' | 'CONCUR_JSON'
      tenant_id    — UUID of the Organization
      file         — (optional) uploaded file
      raw_payload  — (optional) raw CSV or JSON string
    """

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request: Request) -> Response:
        serializer = IngestRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        source_type: str = data["source_type"]
        tenant_id = data["tenant_id"]

        tenant = get_object_or_404(Organization, pk=tenant_id)
        source_system = _get_or_create_source_system(tenant, source_type)

        # Resolve the raw content string
        if data.get("file"):
            uploaded = data["file"]
            filename = uploaded.name
            raw_content = uploaded.read().decode("utf-8", errors="replace")
        else:
            filename = f"{source_type.lower()}_payload"
            raw_content = data["raw_payload"]

        try:
            if source_type == "SAP_EXPORT":
                parser = SAPExportParser(tenant=tenant, source_system=source_system)
                result = parser.parse(raw_content, filename=filename)

            elif source_type == "UTILITY_CSV":
                parser = UtilityCSVParser(tenant=tenant, source_system=source_system)
                result = parser.parse(raw_content, filename=filename)

            elif source_type == "CONCUR_JSON":
                parser = CorporateTravelParser(tenant=tenant, source_system=source_system)
                # Accept either a JSON string or a pre-parsed dict (when sent as JSON body)
                payload = raw_content
                if isinstance(raw_content, str):
                    try:
                        payload = json.loads(raw_content)
                    except json.JSONDecodeError:
                        pass  # parser handles the error internally
                result = parser.parse(payload, filename=filename)

            else:
                return Response(
                    {"detail": f"Unsupported source_type: {source_type}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Exception as exc:
            logger.exception("Unhandled error during ingestion of %s", source_type)
            return Response(
                {"detail": f"Internal ingestion error: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        response_body = {
            "raw_log_id": str(result.raw_log.id),
            "processed_status": result.raw_log.processed_status,
            "records_created": len(result.created_records),
            "row_errors": result.row_errors,
        }
        http_status = (
            status.HTTP_201_CREATED
            if result.success
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        return Response(response_body, status=http_status)


# ---------------------------------------------------------------------------
# GET /api/dashboard/records/
# ---------------------------------------------------------------------------

class DashboardRecordsView(APIView):
    """
    Returns normalized emission records.

    Query params (all optional):
      tenant_id      — filter by Organization UUID
      status         — filter by RecordStatus value
      scope_category — filter by ScopeCategory value
      page           — 1-based page number (default 1)
      page_size      — records per page (default 50, max 200)
    """

    def get(self, request):
        tenant_id = request.query_params.get("tenant_id")

        if not tenant_id:
            return Response(
            {"detail": "tenant_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
        
        qs = NormalizedEmissionActivity.objects.select_related(
            "tenant", "raw_log"
        ).filter(tenant_id=tenant_id)

        record_status=request.query_params.get("status")
        if record_status:
            qs = qs.filter(status=record_status)

        scope = request.query_params.get("scope_category")
        if scope:
            qs = qs.filter(scope_category=scope)

        # Simple manual pagination
        try:
            page = max(1, int(request.query_params.get("page", 1)))
            page_size = min(200, max(1, int(request.query_params.get("page_size", 50))))
        except ValueError:
            return Response(
                {"detail": "page and page_size must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        total = qs.count()
        offset = (page - 1) * page_size
        records = qs[offset : offset + page_size]

        serializer = NormalizedEmissionActivitySerializer(records, many=True)
        return Response(
            {
                "total": total,
                "page": page,
                "page_size": page_size,
                "results": serializer.data,
            }
        )


# ---------------------------------------------------------------------------
# POST /api/records/<id>/approve/
# ---------------------------------------------------------------------------

class ApproveRecordView(APIView):
    """
    Transitions a record to APPROVED.
    Once approved, the record becomes immutable (enforced at model level too).
    """

    @transaction.atomic
    def post(self, request: Request, pk: str) -> Response:
        record = get_object_or_404(NormalizedEmissionActivity, pk=pk)

        if record.status == NormalizedEmissionActivity.RecordStatus.APPROVED:
            return Response(
                {"detail": "Record is already approved."},
                status=status.HTTP_409_CONFLICT,
            )

        if record.status == NormalizedEmissionActivity.RecordStatus.REJECTED:
            return Response(
                {"detail": "Rejected records cannot be approved. Create a new record instead."},
                status=status.HTTP_409_CONFLICT,
            )

        # Bypass the immutability guard by using queryset update (no model.save())
        NormalizedEmissionActivity.objects.filter(pk=pk).update(
            status=NormalizedEmissionActivity.RecordStatus.APPROVED
        )

        AuditLog.objects.create(
            activity_record=record,
            changed_by_user=request.user if request.user.is_authenticated else None,
            field_name="status",
            old_value=record.status,
            new_value=NormalizedEmissionActivity.RecordStatus.APPROVED,
            reason_for_change="Record approved via API.",
        )

        return Response({"detail": "Record approved.", "id": str(pk)})


# ---------------------------------------------------------------------------
# POST /api/records/<id>/reject/
# ---------------------------------------------------------------------------

class RejectRecordView(APIView):
    """Transitions a record to REJECTED."""

    @transaction.atomic
    def post(self, request: Request, pk: str) -> Response:
        serializer = RejectRecordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        record = get_object_or_404(NormalizedEmissionActivity, pk=pk)

        if record.status == NormalizedEmissionActivity.RecordStatus.APPROVED:
            return Response(
                {"detail": "Approved records cannot be rejected."},
                status=status.HTTP_409_CONFLICT,
            )

        if record.status == NormalizedEmissionActivity.RecordStatus.REJECTED:
            return Response(
                {"detail": "Record is already rejected."},
                status=status.HTTP_409_CONFLICT,
            )

        old_status = record.status
        NormalizedEmissionActivity.objects.filter(pk=pk).update(
            status=NormalizedEmissionActivity.RecordStatus.REJECTED
        )

        AuditLog.objects.create(
            activity_record=record,
            changed_by_user=request.user if request.user.is_authenticated else None,
            field_name="status",
            old_value=old_status,
            new_value=NormalizedEmissionActivity.RecordStatus.REJECTED,
            reason_for_change=serializer.validated_data["reason_for_change"],
        )

        return Response({"detail": "Record rejected.", "id": str(pk)})


# ---------------------------------------------------------------------------
# PATCH /api/records/<id>/edit/
# ---------------------------------------------------------------------------

class EditRecordView(APIView):
    """
    Allows an analyst to correct original_value and original_unit.

    Automatically writes an AuditLog entry for every changed field.
    Blocked on APPROVED records (returns 409).
    """

    @transaction.atomic
    def patch(self, request: Request, pk: str) -> Response:
        serializer = EditRecordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        record = get_object_or_404(NormalizedEmissionActivity, pk=pk)

        if record.status == NormalizedEmissionActivity.RecordStatus.APPROVED:
            return Response(
                {"detail": "Approved records are immutable and cannot be edited."},
                status=status.HTTP_409_CONFLICT,
            )

        data = serializer.validated_data
        reason = data["reason_for_change"]
        audit_entries = []

        # Track and apply changes field by field
        new_original_value = data["original_value"]
        new_original_unit = data["original_unit"]

        if record.original_value != new_original_value:
            audit_entries.append(
                AuditLog(
                    activity_record=record,
                    changed_by_user=request.user if request.user.is_authenticated else None,
                    field_name="original_value",
                    old_value=str(record.original_value),
                    new_value=str(new_original_value),
                    reason_for_change=reason,
                )
            )

        if record.original_unit != new_original_unit:
            audit_entries.append(
                AuditLog(
                    activity_record=record,
                    changed_by_user=request.user if request.user.is_authenticated else None,
                    field_name="original_unit",
                    old_value=record.original_unit,
                    new_value=new_original_unit,
                    reason_for_change=reason,
                )
            )

        if not audit_entries:
            return Response(
                {"detail": "No changes detected."},
                status=status.HTTP_200_OK,
            )

        # Persist changes — use queryset update to avoid triggering the
        # immutability guard in model.save() (status is not APPROVED here,
        # but we keep the pattern consistent)
        NormalizedEmissionActivity.objects.filter(pk=pk).update(
            original_value=new_original_value,
            original_unit=new_original_unit,
        )

        AuditLog.objects.bulk_create(audit_entries)

        record.refresh_from_db()
        return Response(
            {
                "detail": "Record updated.",
                "id": str(pk),
                "audit_entries_written": len(audit_entries),
                "record": NormalizedEmissionActivitySerializer(record).data,
            }
        )
