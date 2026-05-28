# MODEL.md — Data Model & Design Rationale

## Overview

The schema is built around one central question: **for any CO₂e number in the system, can an auditor trace it back to the exact byte of raw data that produced it, know who touched it, and prove it hasn't been silently altered?**

Every design decision flows from that requirement.

---

## Entity Relationship Summary

```
Organization (tenant)
    │
    ├── SourceSystem (1..n per tenant)
    │       └── RawIngestionLog (1..n per source system)
    │               └── NormalizedEmissionActivity (1..n per log)
    │                       └── AuditLog (0..n per activity)
    └── (direct FK on all tables for row-level tenant isolation)
```

---

## Table-by-Table Rationale

### 1. `organization` — The Tenant Boundary

```
id          UUID PK   (non-sequential, safe to expose in URLs)
name        VARCHAR   UNIQUE
created_at  TIMESTAMPTZ
```

**Why UUID PK?** Sequential integer PKs leak record counts to tenants. UUIDs are opaque and safe to include in API responses and URLs without revealing business intelligence about other tenants.

**Why a separate tenant table instead of a `tenant_id` column on every table?** It gives a single place to add tenant-level config (billing tier, emission factor overrides, reporting currency) without a schema migration on every downstream table.

**Multi-tenancy enforcement:** Every other table carries a `tenant` FK. All querysets in `views.py` filter by `tenant_id` from the request. There is no cross-tenant data path in the ORM layer.

---

### 2. `source_system` — Data Stream Registry

```
id           UUID PK
tenant       FK → organization
name         VARCHAR
source_type  ENUM: SAP_EXPORT | UTILITY_CSV | CONCUR_JSON
created_at   TIMESTAMPTZ
UNIQUE(tenant, name)
```

**Why a separate table?** A source system is a configuration object, not just a label. In production it would carry credentials, polling schedules, schema version, and contact info for the data owner. Keeping it as a FK means you can query "all records from SAP Plant A" without scanning the payload snapshots.

**Why `source_type` as an enum?** The three types drive fundamentally different parsing logic. The enum is the discriminator that routes an upload to the correct parser class. Adding a new source type is a one-line enum addition + a new parser class — no schema change needed.

---

### 3. `raw_ingestion_log` — The Immutable Intake Receipt

```
id                    UUID PK
tenant                FK → organization
source_system         FK → source_system (SET_NULL on delete)
filename              VARCHAR
raw_payload_snapshot  JSONB
processed_status      ENUM: PENDING | SUCCESS | PARTIAL | FAILED
error_message         TEXT
created_at            TIMESTAMPTZ
```

**This is the most important table for data lineage.**

Every upload — whether it succeeds, partially succeeds, or fails — creates exactly one `RawIngestionLog` row before any parsing begins. This means:

- You can always answer "what file produced record X?" by following `NormalizedEmissionActivity.raw_log_id`.
- You can re-parse historical data by replaying `raw_payload_snapshot`.
- You can audit the exact state of the source data at ingestion time, even if the upstream system later corrects or deletes it.

**Why `raw_payload_snapshot` as JSONB?** The three sources have completely different shapes (CSV string, CSV string, JSON object). JSONB stores them all without a separate blob store, keeps them queryable, and avoids the operational complexity of S3/GCS for an MVP. In production, large payloads would be offloaded to object storage with a pointer stored here.

**Why `SET_NULL` on `source_system` delete?** Deleting a source system configuration should not cascade-delete the historical intake receipts. The lineage record must survive the configuration record.

**Why `PROTECT` on `NormalizedEmissionActivity.raw_log`?** The inverse: you cannot delete a raw log if normalized records still point to it. This prevents accidental lineage destruction.

---

### 4. `normalized_emission_activity` — The Canonical Emission Record

```
id                           UUID PK
tenant                       FK → organization
raw_log                      FK → raw_ingestion_log  (PROTECT)
scope_category               ENUM: SCOPE_1 | SCOPE_2 | SCOPE_3
activity_type                ENUM: FUEL_COMBUSTION | ELECTRICITY_CONSUMPTION | BUSINESS_TRAVEL
start_date                   DATE
end_date                     DATE
original_value               DECIMAL(18,4)
original_unit                VARCHAR(20)
normalized_value_liters_or_kwh  FLOAT
calculated_co2e_kg           FLOAT
facility_code                VARCHAR(100)
status                       ENUM: PENDING_REVIEW | FLAGGED_ANOMALY | APPROVED | REJECTED
anomaly_reason               TEXT nullable
created_at                   TIMESTAMPTZ
updated_at                   TIMESTAMPTZ
```

**Scope categorization (GHG Protocol alignment):**

| Scope | Meaning | Activity Type in this system |
|-------|---------|------------------------------|
| SCOPE_1 | Direct emissions from owned/controlled sources | FUEL_COMBUSTION |
| SCOPE_2 | Indirect emissions from purchased electricity | ELECTRICITY_CONSUMPTION |
| SCOPE_3 | All other indirect emissions in the value chain | BUSINESS_TRAVEL |

The `scope_category` + `activity_type` pair is redundant by design — scope is the GHG Protocol classification, activity type is the operational classification. They can diverge (e.g., purchased steam is Scope 2 but not electricity consumption). Keeping both allows filtering by either axis independently.

**Why preserve `original_value` and `original_unit` verbatim?**

The source data is the ground truth. If an analyst later disputes the normalized figure, they need to see exactly what came in. `original_value` uses `DECIMAL(18,4)` — not float — to avoid binary floating-point representation errors on the source number. The normalized value uses `FLOAT` because it is a derived calculation, not a source fact.

**Unit normalization strategy:**

| Source | Input unit | Canonical unit | Conversion |
|--------|-----------|----------------|------------|
| SAP fuel | L or GAL | Liters | GAL × 3.78541 |
| Utility | KWH | kWh | 1:1 |
| Travel flight | KM (route distance) | Passenger-km | distance × passengers |
| Travel hotel | ROOM_NIGHTS | Room-nights | 1:1 |

The canonical unit is stored in `normalized_value_liters_or_kwh`. The field name is intentionally descriptive rather than abstract — it makes the unit unambiguous without requiring a separate unit column.

**Why `start_date` / `end_date` instead of a single `date`?**

Utility bills cover billing cycles that cross calendar month boundaries (e.g., April 12 – May 11). A single date would force an arbitrary assignment to one month, distorting monthly reporting. The date range is preserved verbatim from the source.

**Immutability of APPROVED records:**

Approved records are locked at two layers:
1. `model.save()` checks the existing DB status and raises `ValidationError` if it is `APPROVED`.
2. All API mutation endpoints (`/edit/`, `/approve/`, `/reject/`) use `queryset.update()` rather than `model.save()` to bypass the guard when intentionally transitioning status — but the `/edit/` endpoint explicitly blocks approved records with a 409 before reaching the update.

This two-layer approach means the immutability holds even if someone calls `model.save()` directly from a management command or shell.

**Status state machine:**

```
PENDING_REVIEW ──► APPROVED  (locked, immutable)
PENDING_REVIEW ──► REJECTED
FLAGGED_ANOMALY ──► APPROVED
FLAGGED_ANOMALY ──► REJECTED
FLAGGED_ANOMALY ──► PENDING_REVIEW  (via edit that clears the anomaly)
```

APPROVED → anything is blocked. REJECTED → APPROVED is blocked (re-ingest instead).

**Indexes:**

```sql
INDEX (tenant_id, status)          -- dashboard filter: "show me all pending for tenant X"
INDEX (tenant_id, scope_category)  -- reporting filter: "Scope 2 total for tenant X"
INDEX (facility_code)              -- anomaly detection: historical avg per facility
```

---

### 5. `audit_log` — The Append-Only Change Journal

```
id                UUID PK
activity_record   FK → normalized_emission_activity  (PROTECT)
changed_by_user   FK → auth_user  (SET_NULL)
field_name        VARCHAR(100)
old_value         TEXT nullable
new_value         TEXT nullable
timestamp         TIMESTAMPTZ  (auto_now_add)
reason_for_change TEXT
```

**Why one row per field changed, not one row per edit event?**

Granular field-level rows make it trivial to answer "what was the original_value before any edits?" without parsing a JSON diff blob. Each row is self-contained and queryable.

**Why `PROTECT` on `activity_record` delete?** You cannot delete an emission record that has been edited. The audit trail must outlive the record it describes.

**Why `SET_NULL` on `changed_by_user` delete?** Deleting a user account should not cascade-delete the audit history. The change happened; the actor's account being gone doesn't un-happen it.

**Why no `updated_at` on AuditLog?** Audit logs are append-only. `auto_now_add` on `timestamp` is intentional — there is no update path.

**Admin enforcement:** The Django admin registers `AuditLog` with `has_add_permission`, `has_change_permission`, and `has_delete_permission` all returning `False`. The table is read-only in the admin UI.

---

## Source-of-Truth Tracking Summary

For any `NormalizedEmissionActivity` row, the full provenance chain is:

```
record.raw_log          → which upload event produced this row
record.raw_log.filename → what file was uploaded
record.raw_log.raw_payload_snapshot → the exact bytes that were parsed
record.raw_log.source_system → which configured data stream
record.raw_log.created_at → when it arrived
record.audit_logs.all() → every human modification since creation
```

This chain is enforced by `PROTECT` foreign keys — no link in it can be silently broken.
