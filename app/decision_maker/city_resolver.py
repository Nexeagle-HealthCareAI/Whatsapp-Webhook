import logging
from typing import Any
from app.decision_maker.geo import haversine_km

logger = logging.getLogger("city_resolver")


def build_from_doctors(doctors: list[dict[str, Any]]) -> dict[str, list[list[float]]]:
    """city -> list of distinct [lat, lng] clusters. Pure function so it can be tested
    against fixture data without any network."""
    index: dict[str, list[list[float]]] = {}
    for doctor in doctors:
        city = (doctor.get("city") or "").strip()
        lat, lng = doctor.get("latitude"), doctor.get("longitude")
        if not city or lat is None or lng is None:
            # Real data has both: records with null coordinates, and at least one junk city
            # value. Skipping is right — a city with no usable coordinate can't be resolved
            # to anyway, and keeping it would only add a never-matching entry.
            continue
        point = [round(float(lat), 4), round(float(lng), 4)]
        points = index.setdefault(city, [])
        if point not in points:
            points.append(point)
    return index


def nearest_city(index: dict[str, list[list[float]]], lat: float, lng: float) -> tuple[str | None, float]:
    """(city_name, distance_km) for the closest city, by its nearest coordinate cluster."""
    best_city, best_km = None, float("inf")
    for city, points in index.items():
        for point in points:
            distance = haversine_km(lat, lng, point[0], point[1])
            if distance < best_km:
                best_city, best_km = city, distance
    return best_city, best_km


def cities_within(
    index: dict[str, list[list[float]]], lat: float, lng: float, radius_km: float, limit: int
) -> list[tuple[str, float]]:
    """Every city with at least one coordinate cluster inside radius_km, nearest first.

    Plural on purpose. A patient near a district border may well have the closest doctors in
    the next town over, and filtering by only their own city name would hide those. Returning
    every city in range lets the caller pull doctors from all of them and then rank on true
    per-doctor distance.

    A city qualifies on its NEAREST cluster, so a town whose records are split across good
    and bad coordinates still qualifies on its good ones — the bad-coordinate doctors are
    then dropped later by their own distance, not by excluding the whole town."""
    in_range: list[tuple[str, float]] = []
    for city, points in index.items():
        nearest = min(
            (haversine_km(lat, lng, point[0], point[1]) for point in points), default=float("inf")
        )
        if nearest <= radius_km:
            in_range.append((city, nearest))
    in_range.sort(key=lambda pair: pair[1])
    return in_range[:limit]


def match_typed_city(index: dict[str, list[list[float]]], text: str) -> str | None:
    """Maps what a patient typed onto a city name the API will actually match.

    `city=` is case-insensitive but exact, so "kishanganj" is fine while "Kishan" or
    "Kishanganj Bihar" return zero doctors. Rather than pass unvalidated free text straight
    through and show an empty list, only return a value that's known to exist in the index;
    anything else returns None and the caller simply doesn't filter by city."""
    typed = (text or "").strip().lower()
    if not typed:
        return None
    for city in index:
        if city.lower() == typed:
            return city
    # Tolerate the common "<city> <state>" / "near <city>" phrasings by looking for a known
    # city name inside what was typed. Longest first, so "Delhi NCR" wins over "Delhi".
    for city in sorted(index, key=len, reverse=True):
        if city.lower() in typed:
            return city
    # And the reverse: a patient typing a shortened or partial name ("Kishan", "Delhi" when
    # 1HMS files it as "Delhi NCR"). The API itself rejects partials, so resolving them here
    # to a full name is the difference between a working search and an empty list. Floor of
    # 4 characters keeps this from matching on noise.
    if len(typed) >= 4:
        for city in sorted(index, key=len):
            if city.lower().startswith(typed):
                return city
    return None
