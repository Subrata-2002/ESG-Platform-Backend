"""URL patterns for the ingestion app."""

from django.urls import path

from .views import (
    ApproveRecordView,
    DashboardRecordsView,
    EditRecordView,
    IngestView,
    RejectRecordView,
)

urlpatterns = [
    path("ingest/", IngestView.as_view(), name="ingest"),
    path("dashboard/records/", DashboardRecordsView.as_view(), name="dashboard-records"),
    path("records/<str:pk>/approve/", ApproveRecordView.as_view(), name="record-approve"),
    path("records/<str:pk>/reject/", RejectRecordView.as_view(), name="record-reject"),
    path("records/<str:pk>/edit/", EditRecordView.as_view(), name="record-edit"),
]
