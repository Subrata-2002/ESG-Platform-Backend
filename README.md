# ESG Sustainability Platform — Backend

An enterprise-grade ESG data ingestion and normalization API built with Django + Django REST Framework. It ingests raw emissions data from three distinct real-world source formats, normalizes everything into a single auditable record shape, and exposes a review workflow (approve / reject / edit) with a full append-only audit trail.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Core Features](#2-core-features)
3. [Tech Stack](#3-tech-stack)
4. [Project Structure](#4-project-structure)
5. [Database Tables](#5-database-tables)
6. [API Overview](#6-api-overview)
7. [Request Flow](#7-request-flow)
8. [Emission Factors & GHG Protocol Alignment](#8-emission-factors--ghg-protocol-alignment)
9. [Anomaly Detection](#9-anomaly-detection)
10. [Local Setup](#10-local-setup)
11. [Environment Variables](#11-environment-variables)
12. [Sample Payloads & cURL Examples](#12-sample-payloads--curl-examples)
13. [Deployment](#13-deployment)
14. [Known Limitations & Tradeoffs](#14-known-limitations--tradeoffs)

---

## 1. Project Overview

The platform solves a core ESG reporting problem: organizations receive emissions-relevant data from multiple disconnected systems — ERP exports, utility portals, and travel management platforms — each with a completely different format, unit system, and data quality level. Before any CO₂e figure can appear in a sustainability report, it must be:

- **Ingested** from the raw source without data loss
- **Normalized** to a canonical unit and emission factor
- **Reviewed** by a human analyst who can correct errors
- **Approved** and locked so the audited figure cannot be silently changed
- **Traced** back to the exact raw byte that produced it

This backend handles all five steps. Every record carries a lineage pointer to its raw source, every human edit writes an immutable audit log entry, and approved records are locked at both the model and API layers.

The system is multi-tenant: every piece of data is scoped to an `Organization`, so a single deployment can serve multiple companies without cross-tenant data leakage.

---

## 2. Core Features

**Multi-source ingestion**
- SAP ALV Grid CSV export (Scope 1 — fuel combustion, German column headers, compact BUDAT date format)
- Utility portal billing CSV (Scope 2 — electricity, cross-month billing periods, Peak/Off-Peak tiers)
- Navan / Concur JSON webhook (Scope 3 — business travel, flight segments + hotel stays)

**GHG Protocol alignment**
- Scope 1, 2, and 3 categorization on every record
- Emission factors sourced from IPCC AR6, US EPA, and DEFRA 2023

**Data lineage**
- Every upload creates an immutable `RawIngestionLog` with a full payload snapshot before any parsing begins
- Every `NormalizedEmissionActivity` carries a `raw_log` FK — the chain from CO₂e figure back to raw bytes is always intact

**Review workflow**
- Records land in `PENDING_REVIEW` or `FLAGGED_ANOMALY` after ingestion
- Analysts can approve, reject, or correct `original_value` / `original_unit`
- Approved records are immutable — blocked at both model `save()` and API layer

**Append-only audit trail**
- Every human edit writes one `AuditLog` row per changed field
- Stores old value, new value, who changed it, and a mandatory reason
- `AuditLog` rows are protected from deletion by `PROTECT` FK

**Anomaly detection**
- Utility CSV parser compares each row against the all-time historical average for that facility
- Rows exceeding 200% of the average are flagged as `FLAGGED_ANOMALY` with a human-readable reason

**Multi-tenancy**
- Row-level isolation: every table carries a `tenant` FK
- All querysets filter by tenant — no cross-tenant data path in the ORM layer

**Partial success handling**
- A single bad row never aborts the entire file
- Malformed rows become `FLAGGED_ANOMALY` placeholder records; the rest of the file processes normally
- The `RawIngestionLog` status reflects `PARTIAL` when some rows failed

---

## 3. Tech Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Language | Python | 3.11+ |
| Web framework | Django | 5.2 |
| API framework | Django REST Framework | 3.17 |
| Database (production) | PostgreSQL via NeonDB (serverless) | — |
| Database (local dev) | SQLite | — |
| DB connection | dj-database-url | 3.1 |
| PostgreSQL driver | psycopg2-binary | 2.9 |
| CORS | django-cors-headers | 4.9 |
| Environment config | python-dotenv | 1.2 |
| Production server | Gunicorn | 21.2 |
| Deployment | Render | — |

---

## 4. Project Structure

```
Assignment/
├── backend/                        ← Django project root
│   ├── esg_platform/               ← Django project package
│   │   ├── settings.py             ← All configuration (DB, CORS, DRF, logging)
│   │   ├── urls.py                 ← Root URL conf — mounts /admin/ and /api/
│   │   ├── wsgi.py                 ← WSGI entry point (Gunicorn)
│   │   └── asgi.py                 ← ASGI entry point (future async support)
│   │
│   ├── ingestion/                  ← Core application
│   │   ├── models.py               ← Organization, SourceSystem, RawIngestionLog,
│   │   │                               NormalizedEmissionActivity, AuditLog
│   │   ├── services.py             ← Three parser classes (SAP, Utility, Travel)
│   │   ├── views.py                ← Five API views (ingest, dashboard, approve,
│   │   │                               reject, edit)
│   │   ├── serializers.py          ← DRF serializers for all request/response shapes
│   │   ├── urls.py                 ← App-level URL patterns
│   │   ├── constants.py            ← Emission factors, airport distances, thresholds
│   │   ├── admin.py                ← Django admin registrations (AuditLog read-only)
│   │   ├── apps.py                 ← App config
│   │   ├── tests.py                ← Test suite
│   │   ├── migrations/             ← Django migrations
│   │   └── sample_payloads/        ← Example input files for all three source types
│   │       └── business_travel_navan_concur.json
│   │
│   ├── manage.py                   ← Django management CLI
│   ├── smoke_test.py               ← End-to-end smoke test script
│   ├── requirements.txt            ← Pinned Python dependencies
│   ├── build.sh                    ← Render build script (migrate + collectstatic)
│   ├── .env                        ← Local environment variables (not committed)
│   ├── MODEL.md                    ← Data model design rationale
│   ├── DECISIONS.md                ← Ambiguity resolution log
│   ├── TRADEOFFS.md                ← Deliberate omissions and their justifications
│   └── SOURCES.md                  ← Emission factor sources
│
└── frontend/                       ← Static SPA (no build step)
    ├── index.html
    ├── styles.css
    └── app.js
```

---

## 5. Database Tables

Five tables, all scoped to a tenant. The lineage chain flows top-to-bottom and is enforced by `PROTECT` foreign keys.

```
organization
    │
    ├── source_system  (1..n per org)
    │       └── raw_ingestion_log  (1..n per source system)
    │               └── normalized_emission_activity  (1..n per log)
    │                       └── audit_log  (0..n per activity)
    └── (direct tenant FK on all tables for row-level isolation)
```

### `organization` — Tenant boundary

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | Non-sequential; safe to expose in URLs |
| `name` | VARCHAR(255) | Unique across all tenants |
| `created_at` | TIMESTAMPTZ | Auto-set on insert |

### `source_system` — Data stream registry

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `tenant` | FK → organization | |
| `name` | VARCHAR(255) | Unique per tenant |
| `source_type` | ENUM | `SAP_EXPORT` \| `UTILITY_CSV` \| `CONCUR_JSON` |
| `created_at` | TIMESTAMPTZ | |

### `raw_ingestion_log` — Immutable intake receipt

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `tenant` | FK → organization | |
| `source_system` | FK → source_system | `SET_NULL` on delete — log survives config deletion |
| `filename` | VARCHAR(512) | Original filename or `source_type_payload` for raw strings |
| `raw_payload_snapshot` | JSONB | Full copy of the raw input before any parsing |
| `processed_status` | ENUM | `PENDING` → `SUCCESS` \| `PARTIAL` \| `FAILED` |
| `error_message` | TEXT | JSON array of row-level errors on `PARTIAL`; full message on `FAILED` |
| `created_at` | TIMESTAMPTZ | |

### `normalized_emission_activity` — Canonical emission record

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `tenant` | FK → organization | |
| `raw_log` | FK → raw_ingestion_log | `PROTECT` — cannot delete log while records exist |
| `scope_category` | ENUM | `SCOPE_1` \| `SCOPE_2` \| `SCOPE_3` |
| `activity_type` | ENUM | `FUEL_COMBUSTION` \| `ELECTRICITY_CONSUMPTION` \| `BUSINESS_TRAVEL` |
| `start_date` | DATE | Billing period start or travel date |
| `end_date` | DATE | Billing period end (equals start_date for point-in-time events) |
| `original_value` | DECIMAL(18,4) | Source value verbatim — never a float to avoid rounding errors |
| `original_unit` | VARCHAR(20) | `L`, `GAL`, `KWH`, `KM`, `ROOM_NIGHTS`, `UNKNOWN` |
| `normalized_value_liters_or_kwh` | FLOAT | Canonical unit: liters (Scope 1), kWh (Scope 2), passenger-km or room-nights (Scope 3) |
| `calculated_co2e_kg` | FLOAT | `normalized_value × emission_factor` |
| `facility_code` | VARCHAR(100) | SAP WERKS / utility facility ID / hotel location |
| `status` | ENUM | `PENDING_REVIEW` \| `FLAGGED_ANOMALY` \| `APPROVED` \| `REJECTED` |
| `anomaly_reason` | TEXT nullable | Human-readable explanation when status is `FLAGGED_ANOMALY` |
| `created_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | Auto-updated on every save |

**Indexes:**
```sql
INDEX (tenant_id, status)           -- dashboard: "all pending records for tenant X"
INDEX (tenant_id, scope_category)   -- reporting: "Scope 2 total for tenant X"
INDEX (facility_code)               -- anomaly detection: historical avg per facility
```

**Status state machine:**
```
PENDING_REVIEW  ──► APPROVED   (locked, immutable)
PENDING_REVIEW  ──► REJECTED
FLAGGED_ANOMALY ──► APPROVED
FLAGGED_ANOMALY ──► REJECTED
```
`APPROVED → anything` is blocked. `REJECTED → APPROVED` is blocked (re-ingest instead).

### `audit_log` — Append-only change journal

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `activity_record` | FK → normalized_emission_activity | `PROTECT` — audit trail outlives the record |
| `changed_by_user` | FK → auth_user | `SET_NULL` — history survives user deletion |
| `field_name` | VARCHAR(100) | One row per field changed, not one row per edit event |
| `old_value` | TEXT nullable | Previous value as string |
| `new_value` | TEXT nullable | New value as string |
| `timestamp` | TIMESTAMPTZ | `auto_now_add` — no update path exists |
| `reason_for_change` | TEXT | Mandatory; minimum 5 characters |

---

## 6. API Overview

Base URL: `http://127.0.0.1:8000/api/` (local) · `https://esg-platform-backend-327e.onrender.com/api/` (production)

| Method | Endpoint | Description | Success status |
|--------|----------|-------------|----------------|
| `POST` | `/api/ingest/` | Upload a file or raw payload for parsing | `201 Created` / `422` on all-row failure |
| `GET` | `/api/dashboard/records/` | List normalized records with filters and pagination | `200 OK` |
| `POST` | `/api/records/<id>/approve/` | Approve a record — makes it immutable | `200 OK` |
| `POST` | `/api/records/<id>/reject/` | Reject a record with a mandatory reason | `200 OK` |
| `PATCH` | `/api/records/<id>/edit/` | Correct `original_value` / `original_unit` | `200 OK` |

### POST `/api/ingest/`

Accepts `multipart/form-data` or `application/json`.

**Request fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_type` | string | yes | `SAP_EXPORT` \| `UTILITY_CSV` \| `CONCUR_JSON` |
| `tenant_id` | UUID | yes | Must match an existing `Organization.id` |
| `file` | file | one of | Uploaded CSV or JSON file |
| `raw_payload` | string | one of | Raw CSV or JSON string |

**Response (201):**
```json
{
  "raw_log_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "processed_status": "SUCCESS",
  "records_created": 10,
  "row_errors": []
}
```

**Response (422 — partial failure):**
```json
{
  "raw_log_id": "...",
  "processed_status": "PARTIAL",
  "records_created": 9,
  "row_errors": [
    { "trip_index": 2, "error": "Unknown trip type 'Train'", "data": { ... } }
  ]
}
```

### GET `/api/dashboard/records/`

**Query parameters (all optional):**

| Param | Description | Example |
|-------|-------------|---------|
| `tenant_id` | Filter by organization UUID | `?tenant_id=abc-123` |
| `status` | Filter by record status | `?status=PENDING_REVIEW` |
| `scope_category` | Filter by GHG scope | `?scope_category=SCOPE_3` |
| `page` | Page number (1-based, default 1) | `?page=2` |
| `page_size` | Records per page (default 50, max 200) | `?page_size=25` |

**Response (200):**
```json
{
  "total": 142,
  "page": 1,
  "page_size": 50,
  "results": [ { ... } ]
}
```

### POST `/api/records/<id>/approve/`

No request body required. Returns `409 Conflict` if already approved or if the record is rejected.

### POST `/api/records/<id>/reject/`

```json
{ "reason_for_change": "Duplicate entry from re-uploaded file." }
```

Returns `409 Conflict` if already approved or already rejected.

### PATCH `/api/records/<id>/edit/`

```json
{
  "original_value": "450.0000",
  "original_unit": "L",
  "reason_for_change": "Source system exported GAL but label was wrong — confirmed liters with plant manager."
}
```

Returns `409 Conflict` on approved records. Writes one `AuditLog` row per changed field.

---

## 7. Request Flow

### Ingestion flow (POST `/api/ingest/`)

```
Client
  │
  ▼
IngestView.post()
  │  1. Validate request fields (IngestRequestSerializer)
  │  2. Resolve tenant (Organization.objects.get_or_404)
  │  3. Get or create SourceSystem for this tenant + source_type
  │  4. Read file bytes or raw_payload string
  │
  ├─► SAP_EXPORT  ──► SAPExportParser.parse()
  ├─► UTILITY_CSV ──► UtilityCSVParser.parse()
  └─► CONCUR_JSON ──► CorporateTravelParser.parse()
            │
            │  Inside each parser (wrapped in @transaction.atomic):
            │  a. Create RawIngestionLog (status=PENDING) — before any row parsing
            │  b. Validate headers / top-level structure
            │  c. For each row / trip segment:
            │     - Parse and validate fields
            │     - Convert to canonical unit
            │     - Apply emission factor → calculated_co2e_kg
            │     - Run anomaly check (Utility CSV only)
            │     - Create NormalizedEmissionActivity (PENDING_REVIEW or FLAGGED_ANOMALY)
            │     - On row error: create FLAGGED_ANOMALY placeholder, append to row_errors
            │  d. Update RawIngestionLog.processed_status → SUCCESS / PARTIAL / FAILED
            │
            ▼
        IngestionResult { raw_log, created_records[], row_errors[] }
            │
            ▼
        HTTP 201 (success/partial) or 422 (all failed)
```

### Review flow (approve / reject / edit)

```
Client
  │
  ▼
ApproveRecordView / RejectRecordView / EditRecordView
  │  1. Validate request body (serializer)
  │  2. Fetch NormalizedEmissionActivity by PK (404 if not found)
  │  3. Check current status — return 409 if transition is blocked
  │  4. Apply change via queryset.update() (bypasses model.save() guard cleanly)
  │  5. Write AuditLog row(s) — one per changed field
  │
  ▼
HTTP 200 with updated record or detail message
```

### Data lineage chain

```
Any CO₂e figure
  └── NormalizedEmissionActivity.raw_log_id
        └── RawIngestionLog.raw_payload_snapshot  ← exact bytes that produced it
        └── RawIngestionLog.source_system          ← which data stream
        └── RawIngestionLog.created_at             ← when it arrived
  └── NormalizedEmissionActivity.audit_logs.all() ← every human edit since creation
```

---

## 8. Emission Factors & GHG Protocol Alignment

All factors are hardcoded in `ingestion/constants.py`. Sources: IPCC AR6, US EPA, DEFRA 2023.

| Scope | Activity | Factor | Unit |
|-------|----------|--------|------|
| Scope 1 | Fuel combustion (diesel/fuel oil) | 2.68 | kg CO₂e / liter |
| Scope 2 | Electricity (US average grid, market-based) | 0.386 | kg CO₂e / kWh |
| Scope 3 | Flight (economy class, average aircraft) | 0.255 | kg CO₂e / passenger-km |
| Scope 3 | Hotel stay (average) | 31.0 | kg CO₂e / room-night |

**Unit normalization:**

| Source | Input unit | Canonical unit | Conversion |
|--------|-----------|----------------|------------|
| SAP fuel | `L` or `GAL` | Liters | GAL × 3.78541 |
| Utility bill | `KWH` | kWh | 1:1 |
| Flight | Route distance (km) | Passenger-km | distance × passengers |
| Hotel | `ROOM_NIGHTS` | Room-nights | 1:1 |

**Flight distance lookup:** 25 common IATA routes are hardcoded in `AIRPORT_DISTANCES_KM`. Unknown routes fall back to `DEFAULT_FLIGHT_DISTANCE_KM = 1500 km` (approximate median domestic US flight). The record is created as `PENDING_REVIEW`, not flagged — the distance is an estimate, not an error.

---

## 9. Anomaly Detection

Currently implemented for Scope 2 (Utility CSV) only.

**Rule:** If `consumption_kwh > 2.0 × historical_average_kwh` for the same `facility_code`, the record is created as `FLAGGED_ANOMALY` with a reason string:

```
Consumption surge detected: 8500.00 kWh is 3.4x the historical average
(2500.00 kWh) for facility 'PLANT-A'.
```

The historical average is computed as `AVG(normalized_value_liters_or_kwh)` across all non-rejected records for that facility and activity type. On the first upload for a facility (no history), no anomaly check is performed.

The threshold (`ANOMALY_SURGE_THRESHOLD = 2.0`) is defined in `constants.py`.

---

## 10. Local Setup

**Prerequisites:** Python 3.11+, pip

```bash
# 1. Clone and enter the backend directory
cd backend

# 2. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment (see section 11)
copy .env.example .env       # then edit .env

# 5. Run migrations
python manage.py migrate

# 6. (Optional) Create a superuser for /admin
python manage.py createsuperuser

# 7. Start the development server
python manage.py runserver
```

The API is available at `http://127.0.0.1:8000/api/`.
Django admin is at `http://127.0.0.1:8000/admin/`.

**Create a tenant via Django shell:**

```python
python manage.py shell
>>> from ingestion.models import Organization
>>> org = Organization.objects.create(name="Acme Corp")
>>> print(org.id)   # copy this UUID for all API calls
```

**Run the smoke test:**

```bash
python smoke_test.py
```

---

## 11. Environment Variables

Create a `.env` file in the `backend/` directory:

```env
# Required for production — omit to fall back to SQLite locally
DATABASE_URL=postgresql://user:password@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require

# Django secret key — change this in production
SECRET_KEY=your-secret-key-here

# Set to False in production
DEBUG=True
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Production only | SQLite fallback | Full PostgreSQL connection string |
| `SECRET_KEY` | Yes (prod) | Insecure default | Django secret key |
| `DEBUG` | No | `True` | Set `False` in production |

---

## 12. Sample Payloads & cURL Examples

### Scope 1 — SAP Export (CSV)

```bash
curl -X POST http://127.0.0.1:8000/api/ingest/ \
  -F "source_type=SAP_EXPORT" \
  -F "tenant_id=<YOUR_TENANT_UUID>" \
  -F "file=@sap_export.csv"
```

Expected CSV format:
```
BUDAT,MENGE,MEINS,WERKS
20260315,500,L,PLANT-A
20260315,200,GAL,PLANT-B
```

### Scope 2 — Utility CSV

```bash
curl -X POST http://127.0.0.1:8000/api/ingest/ \
  -F "source_type=UTILITY_CSV" \
  -F "tenant_id=<YOUR_TENANT_UUID>" \
  -F "file=@utility_bill.csv"
```

Expected CSV format:
```
facility_code,start_date,end_date,consumption_type,consumption_kwh
PLANT-A,2026-04-01,2026-04-30,Peak Consumption,12500.00
PLANT-A,2026-04-01,2026-04-30,Off-Peak Consumption,4300.00
```

### Scope 3 — Business Travel JSON (Navan / Concur)

```bash
curl -X POST http://127.0.0.1:8000/api/ingest/ \
  -F "source_type=CONCUR_JSON" \
  -F "tenant_id=<YOUR_TENANT_UUID>" \
  -F "raw_payload={\"report_id\":\"RPT-001\",\"submitted_by\":\"jane@acme.com\",\"trips\":[{\"type\":\"Flight\",\"origin\":\"JFK\",\"destination\":\"LHR\",\"travel_date\":\"2026-05-10\",\"passengers\":1},{\"type\":\"Hotel\",\"location\":\"London\",\"check_in\":\"2026-05-10\",\"check_out\":\"2026-05-14\",\"room_nights\":4}]}"
```

A full realistic payload with 10 valid segments and 3 intentionally malformed entries is available at:
`ingestion/sample_payloads/business_travel_navan_concur.json`

### Approve / Reject / Edit

```bash
# Approve
curl -X POST http://127.0.0.1:8000/api/records/<RECORD_UUID>/approve/

# Reject
curl -X POST http://127.0.0.1:8000/api/records/<RECORD_UUID>/reject/ \
  -H "Content-Type: application/json" \
  -d '{"reason_for_change": "Duplicate entry from re-uploaded file."}'

# Edit
curl -X PATCH http://127.0.0.1:8000/api/records/<RECORD_UUID>/edit/ \
  -H "Content-Type: application/json" \
  -d '{"original_value": "450.0000", "original_unit": "L", "reason_for_change": "Unit label was wrong — confirmed liters with plant manager."}'

# Dashboard — all Scope 3 pending records, page 1
curl "http://127.0.0.1:8000/api/dashboard/records/?tenant_id=<UUID>&scope_category=SCOPE_3&status=PENDING_REVIEW&page=1&page_size=25"
```

---

## 13. Deployment

The backend is deployed on **Render** as a web service. The `build.sh` script runs on every deploy:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --no-input
```

The production database is **NeonDB** (serverless PostgreSQL). Set `DATABASE_URL` in the Render environment variables dashboard.

Production URL: `https://esg-platform-backend-327e.onrender.com`

CORS is configured to allow the Vercel-hosted frontend (`https://esg-platform-frontend-rouge.vercel.app`) and localhost origins for local development.

---

## 14. Known Limitations & Tradeoffs

Three deliberate omissions were made to keep scope focused on the core ingestion and audit mechanics:

**No authentication layer**
All endpoints are `AllowAny` for the MVP. Production would need tenant-scoped API keys for machine-to-machine ingestion and user accounts with at minimum `analyst` (ingest, edit, reject) and `auditor` (approve, read-only) roles. `djangorestframework-simplejwt` would cover this.

**No CO₂e recalculation on edit**
When an analyst corrects `original_value` or `original_unit` via `/edit/`, the `calculated_co2e_kg` field is not recalculated. The audit log records the source correction, but the derived figure reflects the original calculation. Production behavior should re-run the normalization and emit audit entries for all three derived fields atomically.

**Synchronous ingestion only**
All parsing happens within the HTTP request cycle. For the file sizes in this assignment this is fine, but a real SAP export can be 50,000+ rows. Production would return `202 Accepted` immediately, process in a Celery worker, and expose a status polling endpoint. The `RawIngestionLog.processed_status` field is already designed to serve as the task state.

For the full rationale behind each decision, see `DECISIONS.md`, `TRADEOFFS.md`, and `MODEL.md`.
