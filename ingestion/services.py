"""
ESG Ingestion Service Layer
Three parsers, each handling a distinct real-world data shape:
  1. SAPExportParser      — Scope 1, Fuel Combustion (CSV with German column headers)
  2. UtilityCSVParser     — Scope 2, Electricity (billing-cycle CSV with tier rows)
  3. CorporateTravelParser — Scope 3, Business Travel (JSON webhook from Navan/Concur)
"""

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.db.models import Avg

from .constants import (
    AIRPORT_DISTANCES_KM,
    ANOMALY_SURGE_THRESHOLD,
    DEFAULT_FLIGHT_DISTANCE_KM,
    EMISSION_FACTOR_ELECTRICITY_KG_CO2E_PER_KWH,
    EMISSION_FACTOR_FLIGHT_KG_CO2E_PER_PKM,
    EMISSION_FACTOR_FUEL_KG_CO2E_PER_LITER,
    EMISSION_FACTOR_HOTEL_KG_CO2E_PER_ROOM_NIGHT,
    GALLONS_TO_LITERS,
)
from .models import (
    NormalizedEmissionActivity,
    Organization,
    RawIngestionLog,
    SourceSystem,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared result container
# ---------------------------------------------------------------------------

@dataclass
class IngestionResult:
    """Returned by every parser so callers get a uniform response."""
    raw_log: RawIngestionLog
    created_records: list[NormalizedEmissionActivity] = field(default_factory=list)
    row_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.raw_log.processed_status in (
            RawIngestionLog.ProcessedStatus.SUCCESS,
            RawIngestionLog.ProcessedStatus.PARTIAL,
        )


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _parse_date(value: str) -> date:
    """Try common date formats; raise ValueError with a clear message on failure.

    Handles SAP's compact 8-digit format (YYYYMMDD / BUDAT) in addition to
    standard ISO-8601 and regional variants.
    """
    value = value.strip()
    for fmt in (
        "%Y%m%d",    # SAP BUDAT compact: 20260327
        "%Y-%m-%d",  # ISO-8601:          2026-03-27
        "%d.%m.%Y",  # German locale:     27.03.2026
        "%m/%d/%Y",  # US locale:         03/27/2026
        "%d/%m/%Y",  # EU locale:         27/03/2026
    ):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {value!r}")


def _parse_decimal(value: str) -> Decimal:
    """Strip whitespace/commas and parse to Decimal; raise InvalidOperation on failure."""
    cleaned = value.strip().replace(",", "")
    return Decimal(cleaned)


# ---------------------------------------------------------------------------
# Parser 1: SAP Export (Scope 1 — Fuel Combustion)
# ---------------------------------------------------------------------------

class SAPExportParser:
    """
    Parses a CSV string with German SAP ALV Grid column headers:
      BUDAT  — Posting Date
      MENGE  — Quantity
      MEINS  — Unit of Measure ('L' or 'GAL')
      WERKS  — Plant / Facility Code

    Business rules:
    - 'L'   → use directly as liters
    - 'GAL' → multiply by 3.78541 to convert to liters
    - Negative or malformed MENGE → flag row as FLAGGED_ANOMALY
    - Any row-level error is recorded in row_errors; processing continues
    """

    REQUIRED_COLUMNS = {"BUDAT", "MENGE", "MEINS", "WERKS"}

    def __init__(self, tenant: Organization, source_system: SourceSystem):
        self.tenant = tenant
        self.source_system = source_system

    @transaction.atomic
    def parse(self, csv_string: str, filename: str = "sap_export.csv") -> IngestionResult:
        raw_log = RawIngestionLog.objects.create(
            tenant=self.tenant,
            source_system=self.source_system,
            filename=filename,
            raw_payload_snapshot={"raw_csv": csv_string},
            processed_status=RawIngestionLog.ProcessedStatus.PENDING,
        )

        result = IngestionResult(raw_log=raw_log)
        reader = csv.DictReader(io.StringIO(csv_string))

        # Validate headers before touching any rows
        if not self.REQUIRED_COLUMNS.issubset(set(reader.fieldnames or [])):
            missing = self.REQUIRED_COLUMNS - set(reader.fieldnames or [])
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.FAILED
            raw_log.error_message = f"Missing required SAP columns: {missing}"
            raw_log.save(update_fields=["processed_status", "error_message"])
            return result

        for row_num, row in enumerate(reader, start=2):
            try:
                posting_date = _parse_date(row["BUDAT"])
                unit = row["MEINS"].strip().upper()

                try:
                    quantity = _parse_decimal(row["MENGE"])
                except InvalidOperation:
                    raise ValueError(f"MENGE is not a valid number: {row['MENGE']!r}")

                if quantity < 0:
                    raise ValueError(f"MENGE is negative ({quantity}); cannot represent fuel consumption.")

                # Unit normalisation → liters
                if unit == "L":
                    liters = float(quantity)
                elif unit == "GAL":
                    liters = float(quantity) * GALLONS_TO_LITERS
                else:
                    raise ValueError(f"Unknown unit {unit!r}; expected 'L' or 'GAL'.")

                co2e_kg = liters * EMISSION_FACTOR_FUEL_KG_CO2E_PER_LITER

                record = NormalizedEmissionActivity.objects.create(
                    tenant=self.tenant,
                    raw_log=raw_log,
                    scope_category=NormalizedEmissionActivity.ScopeCategory.SCOPE_1,
                    activity_type=NormalizedEmissionActivity.ActivityType.FUEL_COMBUSTION,
                    start_date=posting_date,
                    end_date=posting_date,
                    original_value=quantity,
                    original_unit=unit,
                    normalized_value_liters_or_kwh=liters,
                    calculated_co2e_kg=co2e_kg,
                    facility_code=row["WERKS"].strip(),
                    status=NormalizedEmissionActivity.RecordStatus.PENDING_REVIEW,
                )
                result.created_records.append(record)

            except (ValueError, KeyError) as exc:
                logger.warning("SAP row %d error: %s | row=%s", row_num, exc, row)
                # Still create a flagged record so the anomaly is visible in the dashboard
                try:
                    posting_date_safe = _parse_date(row.get("BUDAT", "1970-01-01"))
                except ValueError:
                    posting_date_safe = date(1970, 1, 1)

                flagged = NormalizedEmissionActivity.objects.create(
                    tenant=self.tenant,
                    raw_log=raw_log,
                    scope_category=NormalizedEmissionActivity.ScopeCategory.SCOPE_1,
                    activity_type=NormalizedEmissionActivity.ActivityType.FUEL_COMBUSTION,
                    start_date=posting_date_safe,
                    end_date=posting_date_safe,
                    original_value=Decimal("0"),
                    original_unit=row.get("MEINS", "UNKNOWN").strip(),
                    normalized_value_liters_or_kwh=0.0,
                    calculated_co2e_kg=0.0,
                    facility_code=row.get("WERKS", "").strip(),
                    status=NormalizedEmissionActivity.RecordStatus.FLAGGED_ANOMALY,
                    anomaly_reason=str(exc),
                )
                result.created_records.append(flagged)
                result.row_errors.append({"row": row_num, "error": str(exc), "data": dict(row)})

        # Determine overall log status
        if not result.row_errors:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.SUCCESS
        elif result.created_records:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.PARTIAL
        else:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.FAILED

        raw_log.error_message = json.dumps(result.row_errors) if result.row_errors else ""
        raw_log.save(update_fields=["processed_status", "error_message"])
        return result


# ---------------------------------------------------------------------------
# Parser 2: Utility Portal CSV (Scope 2 — Electricity)
# ---------------------------------------------------------------------------

class UtilityCSVParser:
    """
    Parses a commercial electricity bill CSV.

    Expected columns:
      facility_code, start_date, end_date, consumption_type, consumption_kwh

    consumption_type values: 'Peak Consumption', 'Off-Peak Consumption'

    Business rules:
    - Billing periods may span calendar months — preserve start/end dates verbatim.
    - Each tier row (Peak / Off-Peak) becomes its own NormalizedEmissionActivity.
    - Anomaly detection: if consumption > 200% of the historical average for that
      facility, flag as FLAGGED_ANOMALY with reason "Consumption surge detected."
    """

    REQUIRED_COLUMNS = {"facility_code", "start_date", "end_date", "consumption_type", "consumption_kwh"}

    def __init__(self, tenant: Organization, source_system: SourceSystem):
        self.tenant = tenant
        self.source_system = source_system

    def _historical_average_kwh(self, facility_code: str) -> float | None:
        """
        Returns the mean consumption_kwh for approved/pending records at this facility.
        Returns None if there is no history (first upload).
        """
        agg = (
            NormalizedEmissionActivity.objects.filter(
                tenant=self.tenant,
                facility_code=facility_code,
                activity_type=NormalizedEmissionActivity.ActivityType.ELECTRICITY_CONSUMPTION,
            )
            .exclude(status=NormalizedEmissionActivity.RecordStatus.REJECTED)
            .aggregate(avg=Avg("normalized_value_liters_or_kwh"))
        )
        return agg["avg"]  # None if queryset is empty

    @transaction.atomic
    def parse(self, csv_string: str, filename: str = "utility_bill.csv") -> IngestionResult:
        raw_log = RawIngestionLog.objects.create(
            tenant=self.tenant,
            source_system=self.source_system,
            filename=filename,
            raw_payload_snapshot={"raw_csv": csv_string},
            processed_status=RawIngestionLog.ProcessedStatus.PENDING,
        )

        result = IngestionResult(raw_log=raw_log)
        reader = csv.DictReader(io.StringIO(csv_string))

        if not self.REQUIRED_COLUMNS.issubset(set(reader.fieldnames or [])):
            missing = self.REQUIRED_COLUMNS - set(reader.fieldnames or [])
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.FAILED
            raw_log.error_message = f"Missing required utility CSV columns: {missing}"
            raw_log.save(update_fields=["processed_status", "error_message"])
            return result

        for row_num, row in enumerate(reader, start=2):
            try:
                facility_code = row["facility_code"].strip()
                start_date = _parse_date(row["start_date"])
                end_date = _parse_date(row["end_date"])
                consumption_type = row["consumption_type"].strip()

                try:
                    kwh = float(_parse_decimal(row["consumption_kwh"]))
                except InvalidOperation:
                    raise ValueError(f"consumption_kwh is not a valid number: {row['consumption_kwh']!r}")

                if kwh < 0:
                    raise ValueError(f"consumption_kwh is negative ({kwh}).")

                # Anomaly detection against historical average
                status = NormalizedEmissionActivity.RecordStatus.PENDING_REVIEW
                anomaly_reason = None
                historical_avg = self._historical_average_kwh(facility_code)
                if historical_avg is not None and kwh > ANOMALY_SURGE_THRESHOLD * historical_avg:
                    status = NormalizedEmissionActivity.RecordStatus.FLAGGED_ANOMALY
                    anomaly_reason = (
                        f"Consumption surge detected: {kwh:.2f} kWh is "
                        f"{kwh / historical_avg:.1f}x the historical average "
                        f"({historical_avg:.2f} kWh) for facility {facility_code!r}."
                    )

                co2e_kg = kwh * EMISSION_FACTOR_ELECTRICITY_KG_CO2E_PER_KWH

                record = NormalizedEmissionActivity.objects.create(
                    tenant=self.tenant,
                    raw_log=raw_log,
                    scope_category=NormalizedEmissionActivity.ScopeCategory.SCOPE_2,
                    activity_type=NormalizedEmissionActivity.ActivityType.ELECTRICITY_CONSUMPTION,
                    start_date=start_date,
                    end_date=end_date,
                    original_value=Decimal(str(kwh)),
                    original_unit="KWH",
                    normalized_value_liters_or_kwh=kwh,
                    calculated_co2e_kg=co2e_kg,
                    facility_code=facility_code,
                    status=status,
                    anomaly_reason=anomaly_reason,
                )
                result.created_records.append(record)

            except (ValueError, KeyError) as exc:
                logger.warning("Utility row %d error: %s | row=%s", row_num, exc, row)
                result.row_errors.append({"row": row_num, "error": str(exc), "data": dict(row)})

        if not result.row_errors:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.SUCCESS
        elif result.created_records:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.PARTIAL
        else:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.FAILED

        raw_log.error_message = json.dumps(result.row_errors) if result.row_errors else ""
        raw_log.save(update_fields=["processed_status", "error_message"])
        return result


# ---------------------------------------------------------------------------
# Parser 3: Corporate Travel JSON Webhook (Scope 3 — Business Travel)
# ---------------------------------------------------------------------------

class CorporateTravelParser:
    """
    Parses a JSON webhook payload from a travel platform (Navan / Concur style).

    Expected payload shape:
    {
      "report_id": "...",
      "submitted_by": "...",
      "trips": [
        {
          "type": "Flight",
          "origin": "JFK",
          "destination": "LAX",
          "travel_date": "2026-04-15",
          "passengers": 1
        },
        {
          "type": "Hotel",
          "location": "New York",
          "check_in": "2026-04-15",
          "check_out": "2026-04-18",
          "room_nights": 3
        }
      ]
    }

    Business rules:
    - Flight: look up distance from AIRPORT_DISTANCES_KM; fall back to DEFAULT_FLIGHT_DISTANCE_KM.
      normalized_value = passengers × distance_km (passenger-km).
    - Hotel: normalized_value = room_nights.
    - Unknown trip types are flagged as FLAGGED_ANOMALY.
    """

    def __init__(self, tenant: Organization, source_system: SourceSystem):
        self.tenant = tenant
        self.source_system = source_system

    @staticmethod
    def _flight_distance_km(origin: str, destination: str) -> float:
        key = (origin.upper(), destination.upper())
        return AIRPORT_DISTANCES_KM.get(key, DEFAULT_FLIGHT_DISTANCE_KM)

    @transaction.atomic
    def parse(self, payload: dict | str, filename: str = "travel_webhook.json") -> IngestionResult:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raw_log = RawIngestionLog.objects.create(
                    tenant=self.tenant,
                    source_system=self.source_system,
                    filename=filename,
                    raw_payload_snapshot={"raw": payload},
                    processed_status=RawIngestionLog.ProcessedStatus.FAILED,
                    error_message=f"Invalid JSON: {exc}",
                )
                return IngestionResult(raw_log=raw_log)

        raw_log = RawIngestionLog.objects.create(
            tenant=self.tenant,
            source_system=self.source_system,
            filename=filename,
            raw_payload_snapshot=payload,
            processed_status=RawIngestionLog.ProcessedStatus.PENDING,
        )

        result = IngestionResult(raw_log=raw_log)
        trips = payload.get("trips", [])

        if not isinstance(trips, list):
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.FAILED
            raw_log.error_message = "'trips' must be a JSON array."
            raw_log.save(update_fields=["processed_status", "error_message"])
            return result

        for idx, trip in enumerate(trips):
            try:
                trip_type = trip.get("type", "").strip()

                if trip_type == "Flight":
                    origin = trip["origin"].strip().upper()
                    destination = trip["destination"].strip().upper()
                    travel_date = _parse_date(trip["travel_date"])
                    passengers = int(trip.get("passengers", 1))

                    if passengers <= 0:
                        raise ValueError(f"passengers must be positive, got {passengers}.")

                    distance_km = self._flight_distance_km(origin, destination)
                    passenger_km = passengers * distance_km
                    co2e_kg = passenger_km * EMISSION_FACTOR_FLIGHT_KG_CO2E_PER_PKM

                    record = NormalizedEmissionActivity.objects.create(
                        tenant=self.tenant,
                        raw_log=raw_log,
                        scope_category=NormalizedEmissionActivity.ScopeCategory.SCOPE_3,
                        activity_type=NormalizedEmissionActivity.ActivityType.BUSINESS_TRAVEL,
                        start_date=travel_date,
                        end_date=travel_date,
                        original_value=Decimal(str(distance_km)),
                        original_unit="KM",
                        normalized_value_liters_or_kwh=passenger_km,
                        calculated_co2e_kg=co2e_kg,
                        facility_code="",
                        status=NormalizedEmissionActivity.RecordStatus.PENDING_REVIEW,
                        anomaly_reason=None,
                    )
                    result.created_records.append(record)

                elif trip_type == "Hotel":
                    check_in = _parse_date(trip["check_in"])
                    check_out = _parse_date(trip["check_out"])
                    room_nights = int(trip.get("room_nights", 1))

                    if room_nights <= 0:
                        raise ValueError(f"room_nights must be positive, got {room_nights}.")

                    co2e_kg = room_nights * EMISSION_FACTOR_HOTEL_KG_CO2E_PER_ROOM_NIGHT

                    record = NormalizedEmissionActivity.objects.create(
                        tenant=self.tenant,
                        raw_log=raw_log,
                        scope_category=NormalizedEmissionActivity.ScopeCategory.SCOPE_3,
                        activity_type=NormalizedEmissionActivity.ActivityType.BUSINESS_TRAVEL,
                        start_date=check_in,
                        end_date=check_out,
                        original_value=Decimal(str(room_nights)),
                        original_unit="ROOM_NIGHTS",
                        normalized_value_liters_or_kwh=float(room_nights),
                        calculated_co2e_kg=co2e_kg,
                        facility_code=trip.get("location", "").strip(),
                        status=NormalizedEmissionActivity.RecordStatus.PENDING_REVIEW,
                        anomaly_reason=None,
                    )
                    result.created_records.append(record)

                else:
                    raise ValueError(f"Unknown trip type {trip_type!r}; expected 'Flight' or 'Hotel'.")

            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("Travel trip[%d] error: %s | trip=%s", idx, exc, trip)
                # Create a flagged placeholder so the anomaly surfaces in the dashboard
                try:
                    fallback_date = _parse_date(
                        trip.get("travel_date") or trip.get("check_in") or "1970-01-01"
                    )
                except ValueError:
                    fallback_date = date(1970, 1, 1)

                flagged = NormalizedEmissionActivity.objects.create(
                    tenant=self.tenant,
                    raw_log=raw_log,
                    scope_category=NormalizedEmissionActivity.ScopeCategory.SCOPE_3,
                    activity_type=NormalizedEmissionActivity.ActivityType.BUSINESS_TRAVEL,
                    start_date=fallback_date,
                    end_date=fallback_date,
                    original_value=Decimal("0"),
                    original_unit="UNKNOWN",
                    normalized_value_liters_or_kwh=0.0,
                    calculated_co2e_kg=0.0,
                    facility_code="",
                    status=NormalizedEmissionActivity.RecordStatus.FLAGGED_ANOMALY,
                    anomaly_reason=str(exc),
                )
                result.created_records.append(flagged)
                result.row_errors.append({"trip_index": idx, "error": str(exc), "data": trip})

        if not result.row_errors:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.SUCCESS
        elif result.created_records:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.PARTIAL
        else:
            raw_log.processed_status = RawIngestionLog.ProcessedStatus.FAILED

        raw_log.error_message = json.dumps(result.row_errors) if result.row_errors else ""
        raw_log.save(update_fields=["processed_status", "error_message"])
        return result
