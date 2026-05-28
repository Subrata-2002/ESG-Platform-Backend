# TRADEOFFS.md — Three Deliberate Omissions

---

## 1. No authentication or authorization layer

**What was not built:** JWT tokens, API keys, session-based login, role-based access control (analyst vs auditor vs admin), or per-tenant permission scoping.

**Why it was omitted:** The spec focused entirely on data ingestion, normalization, and audit mechanics. Adding an auth layer would have required a user management flow (registration, password reset, token refresh), a permission model (who can approve vs who can only read), and frontend login screens — none of which were in scope. The time cost would have come directly from the quality of the parsing logic and audit trail, which are the core evaluation criteria.

**What the production version needs:** A tenant-scoped API key system for machine-to-machine ingestion (SAP exports are typically automated), plus user accounts with at minimum two roles: `analyst` (can ingest, edit, reject) and `auditor` (can approve, read-only on everything else). Django's built-in permission system plus `djangorestframework-simplejwt` would cover this in roughly a day of work.

**The real risk of omitting it:** Without auth, any caller who knows a tenant UUID can read or modify that tenant's data. This is acceptable for a demo environment, not for production.

---

## 2. No recalculation of `calculated_co2e_kg` after an analyst edit

**What was not built:** When an analyst uses `PATCH /api/records/<id>/edit/` to correct `original_value` or `original_unit`, the `normalized_value_liters_or_kwh` and `calculated_co2e_kg` fields are **not** recalculated. The edit endpoint only updates the raw source fields and writes the audit log.

**Why it was omitted:** Recalculation requires re-running the unit conversion and emission factor logic from the service layer inside the view layer. That creates a coupling between the API and the parser internals that needs careful design — particularly for unit changes (if an analyst changes the unit from GAL to L, the normalization factor changes entirely). Getting this wrong silently produces incorrect CO₂e figures, which is worse than leaving the recalculation as a manual step.

**The correct production behavior:** The edit endpoint should accept the corrected source values, re-run the appropriate normalization function, update all three derived fields atomically, and write audit log entries for every field that changed (including `normalized_value_liters_or_kwh` and `calculated_co2e_kg`). This requires extracting the unit conversion logic into a pure function that both the parser and the edit endpoint can call.

**The real risk of omitting it:** An analyst who corrects a value from 500 GAL to 500 L will see the audit log reflect the change, but the CO₂e figure will still be based on the original GAL calculation (757 liters worth of CO₂e instead of 500 liters worth). This is a data integrity issue that would be caught in a real audit.

---

## 3. No background task queue for large file ingestion

**What was not built:** Celery, Django-Q, or any async task queue. All ingestion happens synchronously within the HTTP request/response cycle.

**Why it was omitted:** For the file sizes in this assignment (hundreds of rows), synchronous processing completes in under a second. Adding Celery requires a broker (Redis or RabbitMQ), a worker process, task result storage, and retry logic — significant infrastructure overhead for a demo.

**What breaks at scale:** A real SAP export for a large manufacturing plant can be 50,000+ rows. A utility company might send a CSV with 12 months of hourly meter readings (8,760 rows per meter, dozens of meters). Processing these synchronously will hit HTTP timeout limits (typically 30s on Render/Railway), leave the client hanging, and produce no partial results if the worker crashes mid-file.

**The correct production behavior:** `POST /api/ingest/` should immediately return a `202 Accepted` with a `task_id`. The actual parsing runs in a Celery worker. A `GET /api/ingest/status/<task_id>/` endpoint lets the client poll for completion. The `RawIngestionLog` row (already created before parsing begins) serves as the persistent task state — its `processed_status` field transitions from `PENDING` → `SUCCESS/PARTIAL/FAILED` as the worker runs. The frontend's progress bar would poll this endpoint rather than tracking upload bytes.
