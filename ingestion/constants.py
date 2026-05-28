"""
Emission factor constants (kg CO₂e per unit).
Sources: IPCC AR6, US EPA, DEFRA 2023 — hardcoded mock values for now.
"""

# Scope 1 — Fuel Combustion
# kg CO₂e per liter of diesel/fuel oil
EMISSION_FACTOR_FUEL_KG_CO2E_PER_LITER = 2.68

# Scope 2 — Electricity (market-based, US average grid)
# kg CO₂e per kWh
EMISSION_FACTOR_ELECTRICITY_KG_CO2E_PER_KWH = 0.386

# Scope 3 — Business Travel
# kg CO₂e per passenger-km (economy class, average aircraft)
EMISSION_FACTOR_FLIGHT_KG_CO2E_PER_PKM = 0.255

# kg CO₂e per room-night (average hotel)
EMISSION_FACTOR_HOTEL_KG_CO2E_PER_ROOM_NIGHT = 31.0

# Unit conversion
GALLONS_TO_LITERS = 3.78541

# Anomaly detection threshold — flag if consumption > this multiple of historical avg
ANOMALY_SURGE_THRESHOLD = 2.0

# Airport IATA code → approximate distance lookup (km, one-way)
# Covers common US domestic + major international routes
AIRPORT_DISTANCES_KM: dict[tuple[str, str], float] = {
    ("JFK", "LAX"): 3983,
    ("LAX", "JFK"): 3983,
    ("JFK", "LHR"): 5570,
    ("LHR", "JFK"): 5570,
    ("ORD", "LAX"): 2805,
    ("LAX", "ORD"): 2805,
    ("SFO", "JFK"): 4139,
    ("JFK", "SFO"): 4139,
    ("ATL", "LAX"): 3108,
    ("LAX", "ATL"): 3108,
    ("DFW", "JFK"): 2440,
    ("JFK", "DFW"): 2440,
    ("ORD", "JFK"): 1190,
    ("JFK", "ORD"): 1190,
    ("SFO", "LAX"): 559,
    ("LAX", "SFO"): 559,
    ("LHR", "CDG"): 344,
    ("CDG", "LHR"): 344,
    ("LHR", "FRA"): 634,
    ("FRA", "LHR"): 634,
    ("SIN", "LHR"): 10841,
    ("LHR", "SIN"): 10841,
    ("NRT", "LAX"): 8756,
    ("LAX", "NRT"): 8756,
    ("DXB", "LHR"): 5490,
    ("LHR", "DXB"): 5490,
}

# Fallback average distance when a route is not in the lookup (km)
DEFAULT_FLIGHT_DISTANCE_KM = 1500
