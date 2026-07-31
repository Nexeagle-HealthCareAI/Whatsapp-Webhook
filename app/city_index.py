"""
app/city_index.py
Turns a patient's shared GPS coordinates into a city name that /public/doctors?city= will
actually match, WITHOUT hard-coding a town list — the index is built from 1HMS's own data.

Why this exists
---------------
GET /public/doctors accepts `city` (case-insensitive, but EXACT — "Kishan" matches nothing)
and `page`, but NOT sorting or radius filtering. Without a city filter the gateway pulls an
arbitrary page of doctors for the whole country and sorts that slice, so "nearest" can miss
the genuinely nearest doctor entirely once a specialty has more doctors than one page. The
fix is to narrow by city server-side first — which needs a coordinates -> city step, since
WhatsApp gives us a lat/long and the API wants a name.

Why "nearest cluster" and not "city centre"
-------------------------------------------
Real 1HMS data does NOT give one clean coordinate per city. In dev, "Kishanganj" carries
three different coordinate pairs across its doctors, one of which sits at 28.7/77.3 with a
Delhi pincode — i.e. a Delhi-area point filed under a Bihar town's name.

Averaging those into a centroid would put "Kishanganj" somewhere around 27.4/82.6, in open
country roughly 500km from the real town, and a patient standing in actual Kishanganj would
then resolve to Purnea instead. So each city keeps ALL of its distinct coordinate clusters,
and a city's distance is the distance to its NEAREST cluster. Kishanganj's good Bihar point
still wins at ~2km regardless of the bad Delhi one sitting in the same bucket.

This also means the index self-heals: if 1HMS cleans that data up later, nothing here needs
to change.
"""

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.geo import haversine_km
from app.redis_client import get_redis

logger = logging.getLogger("city_index")

_REDIS_KEY = "booking:city_index"


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if settings.hms_api_key:
        headers["X-Api-Key"] = settings.hms_api_key
    return headers


async def _fetch_all_doctors() -> list[dict[str, Any]]:
    """Pages through the whole public doctor directory. Only ever called on a cache miss
    (once a day in practice), never on the hot path of a normal booking — see get_index."""
    doctors: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=settings.hms_api_base_url, timeout=30) as client:
        for page in range(1, settings.city_index_max_pages + 1):
            response = await client.get(
                "/public/doctors",
                params={"page": page, "pageSize": settings.city_index_page_size},
                headers=_headers(),
            )
            response.raise_for_status()
            batch = response.json().get("doctors", [])
            doctors.extend(batch)
            if len(batch) < settings.city_index_page_size:
                break
        else:
            logger.warning(
                "Stopped building city index at the %d-page cap — some cities may be missing. "
                "Raise CITY_INDEX_MAX_PAGES if the doctor directory has grown.",
                settings.city_index_max_pages,
            )
    return doctors


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


async def get_index(force_refresh: bool = False) -> dict[str, list[list[float]]]:
    redis = get_redis()
    if not force_refresh:
        cached = await redis.get(_REDIS_KEY)
        if cached:
            return json.loads(cached)

    doctors = await _fetch_all_doctors()
    index = build_from_doctors(doctors)
    if index:
        await redis.set(_REDIS_KEY, json.dumps(index), ex=settings.city_index_ttl_seconds)
        logger.info("Built city index: %d cities from %d doctors", len(index), len(doctors))
    else:
        logger.warning("City index came back empty — leaving cache untouched")
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
