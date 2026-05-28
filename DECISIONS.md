# DECISIONS.md — Ambiguity Resolution Log

Every non-obvious choice made during implementation, what was chosen, why, and what I'd ask the PM.

---

## 1. SAP BUDAT date format: compact integer vs ISO string

**Ambiguity:** The spec said "Posting Date" but didn't specify the wire format. Real SAP ALV Grid exports deliver BUDAT as a raw 8-character integer string (`20260327`), not ISO-8601 (`2026-03-27`). The initial parser only handled ISO strings, causing silent epoch fallback.

**Decision:** Added `%Y%m%d` as the first format tried in `_parse_date`, before ISO-8601. It is tried first because SAP exports are the primary source of this format and early matching avoids unnecessary iterations.

**What I'd ask the PM:** Does the SAP system ever export BUDAT in a locale-specific format (e.g., `27.03.2026` for German locale installations)? The parser handles that too, but the priority order matters if a value like `01.02.2026` is ambiguous between DD.MM.YYYY and MM.DD.YYYY.

---

## 2. Multi-tenancy: row-level vs schema-level isolation

**Ambiguity:** The spec required multi-tenancy but didn't specify the isolation model.

**Decision:** Row-level isolation — every table has a `tenant` FK, and all querysets filter by it. Schema-per-tenant (PostgreSQL schemas) or database-per-tenant were not chosen.

**Why:** Schema-per-tenant requires dynamic schema routing, makes migrations complex (N schemas to migrate), and is overkill for an MVP. Row-level isolation is simpler, well-understood, and sufficient when the API layer consistently enforces the tenant filter. The risk (a missing WHERE clause leaking cross-tenant data) is mitigated by keeping all data access in a single service layer.

**What I'd ask the PM:** What is the expected tenant count and data volume per tenant? If a single tenant will have 100M+ rows, schema isolation becomes worth the operational cost. At <1M rows per tenant, row-level is fine.

---

## 3. Anomaly detection: static threshold vs ML baseline

**Ambiguity:** The spec said "if consumption > 200% of historical average, flag as anomaly." It didn't specify what "historical average" means — rolling window? All-time? Per billing tier?

**Decision:** All-time average of non-rejected records for that `facility_code` + `activity_type` combination, computed at parse time via a single `AVG()` query. No rolling window, no tier separation.

**Why:** A rolling window requires storing timestamps with enough granularity to define the window, and the spec didn't define the window size. All-time average is deterministic, reproducible, and correct for a first implementation. Tier separation (Peak vs Off-Peak) was not implemented because the anomaly check is about total consumption at a facility, not tier distribution.

**What I'd ask the PM:** Should the anomaly threshold be configurable per tenant or per facility? Should the baseline exclude flagged records (currently it does exclude REJECTED but includes FLAGGED_ANOMALY)? What is the expected behavior on the very first upload for a facility — no history means no anomaly check, which could miss a genuinely anomalous first reading.

---

## 4. Flight distance: lookup table vs great-circle calculation

**Ambiguity:** The spec said "mock a simple lookup dictionary." It didn't specify what to do for routes not in the dictionary.

**Decision:** A hardcoded lookup of 25 common routes. For unknown routes, fall back to `DEFAULT_FLIGHT_DISTANCE_KM = 1500` (roughly the median domestic US flight distance). The record is still created as `PENDING_REVIEW`, not flagged — the distance is an estimate, not an error.

**Why:** A great-circle calculation requires airport coordinate data (lat/lon per IATA code), which is a separate dataset. The spec explicitly said "mock a simple lookup." The 1500km fallback is documented in `constants.py` so analysts can see it.

**What I'd ask the PM:** Should unknown routes be flagged as anomalies so analysts can manually verify the distance? In production, would we integrate with a flight data API (e.g., OAG, FlightAware) or use a static IATA coordinate database?

---

## 5. Approved record immutability: model-level vs API-level only

**Ambiguity:** The spec said "approved rows become immutable" but didn't specify where to enforce it.

**Decision:** Enforced at both the model layer (`model.save()` raises `ValidationError`) and the API layer (views return 409 before attempting any write). The model layer uses `queryset.update()` for the approve transition itself to bypass the guard cleanly.

**Why:** API-level-only enforcement is fragile — a management command, a shell session, or a future background task could bypass it. Model-level enforcement is the last line of defense. The dual enforcement is belt-and-suspenders.

**What I'd ask the PM:** Should there be a superuser override path (e.g., a `force_edit` flag for compliance officers correcting a data error post-approval)? Currently there is no override — once approved, the only path is reject + re-ingest.

---

## 6. Raw payload snapshot: stored in DB vs object storage

**Ambiguity:** The spec required `raw_payload_snapshot` as a JSONB field. Large CSV files could be megabytes.

**Decision:** Store in the database as JSONB for the MVP. CSV content is stored as a string under the key `"raw_csv"`. JSON payloads are stored as the parsed object.

**Why:** Keeps the lineage chain entirely within the database — no external dependency on S3/GCS for a read. Acceptable for the file sizes in this assignment. In production, files over ~100KB should be offloaded to object storage with a pointer URL stored in the JSONB field.

**What I'd ask the PM:** What is the expected file size distribution? A single SAP export for a large plant could be 50MB+. Is there a retention policy for raw snapshots (e.g., purge after 7 years per GDPR)?

---

## 7. Emission factors: hardcoded vs configurable per tenant

**Ambiguity:** The spec said "hardcode reasonable mock emission factor constants."

**Decision:** Hardcoded in `constants.py`. All three parsers import from this single module.

**Why:** The spec was explicit. In production, emission factors change annually (IPCC updates, EPA revisions, country-specific grid factors), and different tenants may use different methodologies (location-based vs market-based for Scope 2). A `EmissionFactor` table with `tenant`, `activity_type`, `valid_from`, `valid_to`, and `factor_value` would be the correct production design.

**What I'd ask the PM:** Which emission factor standard should we target — US EPA, DEFRA, GHG Protocol, or tenant-specified? Should historical records be recalculated when factors are updated, or should the factor used at calculation time be frozen with the record?

---

## 8. Authentication: AllowAny in production settings

**Ambiguity:** The spec didn't mention authentication.

**Decision:** `DEFAULT_PERMISSION_CLASSES = [AllowAny]` for the MVP. The setting is commented with "Tighten in production."

**Why:** Adding JWT or OAuth2 would require a user management flow that wasn't in scope. The API is functional and testable without auth.

**What I'd ask the PM:** What is the auth model — per-tenant API keys, SSO/SAML, or user accounts with RBAC? Should analysts and auditors have different permission levels (analysts can edit, auditors can only read and approve)?

---

## 9. What subsets of each source were handled vs ignored

### SAP Export
**Handled:** BUDAT, MENGE, MEINS (L/GAL), WERKS. Compact date format (YYYYMMDD). Negative quantity detection.

**Ignored:** SAP material number (MATNR), cost center (KOSTL), company code (BUKRS), document type (BLART), currency fields. In a real SAP export there are typically 40–80 columns — we only consume the 4 relevant to fuel quantity.

### Utility CSV
**Handled:** facility_code, start_date, end_date, consumption_type (Peak/Off-Peak), consumption_kwh. Cross-month billing periods. Historical anomaly detection.

**Ignored:** Rate codes, tariff tiers beyond Peak/Off-Peak, demand charges (kW vs kWh), power factor, reactive power, account numbers, meter IDs. Real utility bills have 15–30 columns.

### Corporate Travel JSON
**Handled:** Flight segments (origin IATA, destination IATA, travel_date, passengers). Hotel stays (check_in, check_out, room_nights, location). Unknown type flagging.

**Ignored:** Car rental, rail travel, taxi/rideshare, meal expenses, per-diem, trip purpose, cost center allocation, traveler name/ID, booking class (business vs economy affects emission factor significantly), layovers (multi-segment flights treated as direct).
