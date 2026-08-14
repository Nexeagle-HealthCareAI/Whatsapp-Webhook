"""
app/geo.py
Straight-line (great-circle) distance between two lat/long points, used to sort/filter
doctors by "nearest" once the patient has shared a WhatsApp location.

Deliberately not calling out to a mapping API for this — the /public/doctors response
already carries each doctor's hospital latitude/longitude (confirmed live against
1hms-dev-api.nexeagle.com), so a pure-math haversine is enough and adds no external
dependency or cost. Straight-line distance, not road distance — fine for "which doctor is
roughly closest" sorting, not for a turn-by-turn ETA.
"""

import math


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))
