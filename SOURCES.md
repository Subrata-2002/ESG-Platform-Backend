# SOURCES.md — Real-World Format Research

---

## Source 1: SAP ALV Grid Export (Scope 1 — Fuel Combustion)

### What the real-world format looks like

SAP's ALV (ABAP List Viewer) Grid is the standard output format for SAP ERP reports. When exported to spreadsheet or flat file, it produces a tab-delimited or comma-delimited file with column headers that are SAP field technical names — not human-readable labels. These names come from the ABAP Data Dictionary and are typically 4–6 character uppercase codes.

For a fuel consumption report from the Materials Management (MM) or Plant Maintenance (PM) module, the relevant fields are:

| SAP Field | Meaning | Real-world quirks |
|-----------|---------|-------------------|
| `BUDAT` | Posting Date | Exported as `YYYYMMDD` integer string, no separators |
| `MENGE` | Quantity | Locale-formatted: German SAP uses `.` as thousands separator and `,` as decimal (e.g., `1.234,56`) |
| `MEINS` | Base Unit of Measure | SAP internal unit codes: `L` (liters), `GAL` (US gallons), `KG` (kilograms), `M3` (cubic meters) |
| `WERKS` | Plant | 4-character alphanumeric plant code (e.g., `1000`, `DE01`) |
| `MATNR` | Material Number | 18-character left-padded with zeros |
| `KOSTL` | Cost Center | 10-character |
| `BUKRS` | Company Code | 4-character |

### What I learned

The most critical real-world issue is the date format. SAP stores dates internally as integers and exports them as 8-digit strings without separators. This is documented in SAP Note 1175771 and is consistent across all SAP ERP versions. Any parser that expects ISO-8601 will silently fail on every date field.

The second issue is the German number format. A quantity of 1,234.56 liters appears as `1.234,56` in a German-locale SAP system. The parser currently handles comma-stripping (`_parse_decimal` removes commas) but does not handle the German locale inversion. This would break on any SAP system with German locale settings.

The third issue is the unit code vocabulary. SAP uses its own internal unit codes that do not always match ISO units. `GAL` in SAP means US gallons (not Imperial gallons). `M3` means cubic meters. `KG` means kilograms (relevant for LPG/propane by weight). The parser currently only handles `L` and `GAL`.

### Sample data used

```csv
BUDAT,MENGE,MEINS,WERKS
20260115,500,L,PLANT_A
20260116,200,GAL,PLANT_B
20260117,-50,L,PLANT_C
20260118,abc,L,PLANT_D
20260119,1200.5,L,PLANT_E
```

Row 3 (negative quantity) and Row 4 (malformed quantity) are intentional bad data to exercise the anomaly flagging path. Row 5 tests decimal handling.

### What would break in a real deployment

1. **German locale number format** — `1.234,56` would fail `_parse_decimal` because after comma-stripping it becomes `1.23456`, not `1234.56`. Need locale-aware decimal parsing.
2. **Additional SAP unit codes** — `KG` (kilograms of fuel), `M3` (cubic meters of gas), `TON` (metric tons) are common in fuel reporting. Each needs a conversion factor to liters or a separate normalization path.
3. **Multi-line header rows** — Some SAP exports include a report title row and a blank row before the column headers. The `csv.DictReader` would misidentify the first non-header row as the header.
4. **BOM (Byte Order Mark)** — SAP exports from Windows systems often include a UTF-8 BOM (`\xef\xbb\xbf`) at the start of the file. This causes the first column header to be read as `\ufeffBUDAT` instead of `BUDAT`, failing the required-columns check.
5. **Encoding** — SAP systems in non-English locales often export in Windows-1252 or ISO-8859-1, not UTF-8. The current `decode("utf-8", errors="replace")` would silently corrupt special characters in plant names.

---

## Source 2: Utility Portal CSV (Scope 2 — Electricity)

### What the real-world format looks like

Commercial electricity bills from US utility providers (PG&E, ConEd, ComEd, Duke Energy) are available as CSV downloads from their online portals. The format varies by provider but common patterns include:

- **Billing period** that does not align with calendar months. A bill might cover April 12 – May 11 (30 days), not April 1 – April 30.
- **Tiered consumption rows** — most commercial tariffs split consumption into "On-Peak" and "Off-Peak" (or "Peak" and "Super Off-Peak") with different rates per kWh. Each tier appears as a separate row in the CSV.
- **Demand charges** — a separate line for peak demand in kW (not kWh), which is billed differently and is not an energy consumption figure.
- **Power factor adjustment** — some bills include a reactive power row.
- **Multiple meters** — a large facility may have several sub-meters, each with its own rows.

A typical PG&E commercial CSV export looks like:

```
Account Number,Service Address,Billing Period Start,Billing Period End,Meter ID,Usage Type,Usage (kWh),Rate
1234567890,123 Main St,04/12/2026,05/11/2026,MTR001,Peak Consumption,1200,E-19
1234567890,123 Main St,04/12/2026,05/11/2026,MTR001,Off-Peak Consumption,800,E-19
1234567890,123 Main St,04/12/2026,05/11/2026,MTR001,Peak Demand (kW),45.2,E-19
```

### What I learned

The most important real-world issue is the billing cycle misalignment. Utility bills are issued on a meter-read cycle, not a calendar cycle. A company reporting Scope 2 emissions by calendar month must either prorate the bill across months or accept that their monthly figures will be slightly off. The spec correctly identified this as a key challenge. The implementation preserves `start_date` and `end_date` verbatim, which is the right choice — proration is a reporting-layer concern, not an ingestion-layer concern.

The second issue is the demand charge row. `Peak Demand (kW)` is not an energy consumption figure and should not be summed with kWh rows. The current parser does not filter by consumption type — it accepts any `consumption_type` value. In production, demand charge rows would need to be identified and either skipped or stored in a separate field.

### Sample data used

```csv
facility_code,start_date,end_date,consumption_type,consumption_kwh
PLANT_A,2026-04-12,2026-05-11,Peak Consumption,1200
PLANT_A,2026-04-12,2026-05-11,Off-Peak Consumption,800
PLANT_B,2026-04-01,2026-04-30,Peak Consumption,950
PLANT_C,2026-04-01,2026-04-30,Peak Consumption,9500
```

Row 4 (PLANT_C, 9500 kWh) is designed to trigger the anomaly detection if PLANT_C has prior history below 4750 kWh average.

### What would break in a real deployment

1. **Provider-specific column names** — PG&E uses "Usage (kWh)", ConEd uses "Consumption (kWh)", Duke uses "KWH Used". There is no standard. A production parser needs a provider-specific column mapping layer.
2. **Demand charge rows** — `kW` rows mixed with `kWh` rows would inflate the consumption total if not filtered. The current parser would attempt to parse `45.2` kW as kWh.
3. **Multiple meters per facility** — If a facility has 3 sub-meters, the CSV has 3× the rows. The anomaly detection compares against the per-facility average, which would be correct only if the historical data also included all meters. If a new meter is added mid-year, the historical average drops and every reading from the new meter looks like an anomaly.
4. **Negative consumption** — Net metering (solar generation exceeding consumption) produces negative kWh rows. The current parser rejects negative values as errors. Net metering rows should be stored as negative consumption, not flagged.
5. **Date format** — US utility portals typically use `MM/DD/YYYY`. The parser handles this, but some providers use `M/D/YY` (no zero-padding, 2-digit year), which would fail.

---

## Source 3: Corporate Travel JSON Webhook (Scope 3 — Business Travel)

### What the real-world format looks like

Navan (formerly TripActions) and Concur both offer webhook/API integrations that push expense report data when a report is submitted or approved. The actual Navan webhook payload (from their public API documentation) looks like:

```json
{
  "event_type": "expense_report.approved",
  "report_id": "RPT-2026-001",
  "submitted_by": {
    "employee_id": "EMP-12345",
    "email": "jane.doe@acme.com",
    "cost_center": "CC-ENGINEERING"
  },
  "trips": [
    {
      "type": "air",
      "booking_reference": "ABC123",
      "origin": "JFK",
      "destination": "LAX",
      "departure_datetime": "2026-04-15T08:30:00Z",
      "arrival_datetime": "2026-04-15T11:45:00Z",
      "cabin_class": "economy",
      "passengers": 1,
      "carrier": "AA",
      "flight_number": "AA100"
    },
    {
      "type": "hotel",
      "property_name": "Marriott Downtown LA",
      "city": "Los Angeles",
      "country": "US",
      "check_in_date": "2026-04-15",
      "check_out_date": "2026-04-18",
      "room_nights": 3,
      "room_type": "standard"
    }
  ]
}
```

### What I learned

The real Navan/Concur payload uses `"air"` not `"Flight"`, `"hotel"` not `"Hotel"`, `"check_in_date"` not `"check_in"`, and `"departure_datetime"` (ISO-8601 with time) not `"travel_date"`. The spec used simplified field names. The implementation uses the spec's simplified names, which means it would not work against a real Navan webhook without a field mapping layer.

The most significant real-world issue is cabin class. Economy, business, and first class have dramatically different emission factors per passenger-km. The DEFRA 2023 factors are approximately:

| Cabin | kg CO₂e per passenger-km |
|-------|--------------------------|
| Economy | 0.255 |
| Premium Economy | 0.359 |
| Business | 0.510 |
| First | 1.020 |

The current implementation uses a single economy factor for all flights. A business traveler flying business class would have their emissions underreported by 2×.

The second issue is multi-segment flights. A trip from New York to Tokyo via Los Angeles is two flight segments, not one. The current parser treats each segment independently, which is correct — but only if the webhook sends each segment as a separate trip object. Some travel platforms send the entire itinerary as one object with a `segments` array.

### Sample data used

```json
{
  "report_id": "RPT-2026-001",
  "submitted_by": "jane.doe@acme.com",
  "trips": [
    {"type": "Flight", "origin": "JFK", "destination": "LAX", "travel_date": "2026-03-10", "passengers": 2},
    {"type": "Hotel", "location": "Los Angeles", "check_in": "2026-03-10", "check_out": "2026-03-13", "room_nights": 3},
    {"type": "Flight", "origin": "SFO", "destination": "NRT", "travel_date": "2026-03-15", "passengers": 1},
    {"type": "Taxi", "origin": "LAX", "destination": "Downtown"}
  ]
}
```

The `"Taxi"` entry is intentional bad data to exercise the unknown-type flagging path. The SFO→NRT route is not in the lookup table, exercising the fallback distance logic.

### What would break in a real deployment

1. **Field name mismatch** — Real Navan uses `"air"` not `"Flight"`, `"check_in_date"` not `"check_in"`. The parser would flag every real Navan record as an unknown type. A field mapping/normalization layer is needed before the parser.
2. **Cabin class ignored** — Business and first class flights are underreported by 2–4×. This is a material accuracy issue for companies with significant executive travel.
3. **Multi-segment itineraries** — If the platform sends `{"segments": [...]}` instead of flat trip objects, the parser would not find a `"type"` field and would flag the entire trip as unknown.
4. **Car rental** — A common Scope 3 category completely absent from the current implementation. Car rental emissions depend on vehicle type, fuel type, and distance driven — none of which are in the current schema.
5. **Webhook authentication** — Real webhooks include an HMAC signature header (e.g., `X-Navan-Signature`) that must be verified before processing the payload. The current endpoint accepts any POST body without verification, making it vulnerable to spoofed payloads.
6. **Duplicate delivery** — Webhook platforms guarantee at-least-once delivery, not exactly-once. The same report could be delivered twice. Without idempotency checking on `report_id`, the same trip would be ingested twice, doubling the emissions figure.
