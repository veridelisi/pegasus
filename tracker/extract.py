"""
Extraction logic for turning a raw SerpApi google_flights JSON response
(obtained via selected_flights_json pinning) into a single trustworthy
price for one specific flight.

This module makes NO network calls. It is pure functions over a dict,
so it can be exercised repeatedly against a saved sample_response.json
without spending SerpApi credits (see tracker/main.py --from-file).

Extraction hierarchy (see README for the reasoning):
    1. Validate that selected_flights[0] actually describes the expected
       flight (flight number, departure/arrival airport, date, and
       approximate scheduled times). If it doesn't -> FLIGHT_NOT_MATCHED.
    2. If validated, take the MINIMUM price across booking_options[*]
       for that pinned itinerary (there is usually more than one seller).
       This is source "booking_options[i].together.price" (or the
       separate_tickets variant).
    3. Defensive fallback: if booking_options is absent/empty, scan
       best_flights / other_flights (only present if SerpApi ever
       returns them alongside a pinned search) for an entry whose own
       flights[] match PC2476/ESB/ADB/date, and use that entry's own
       "price" field.
    4. If nothing usable was found -> PRICE_NOT_FOUND.

price_insights is deliberately EXCLUDED from this hierarchy. It is
extracted separately (see extract_price_insights) and must never be
used to populate pc2476_price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from tracker.config import ExpectedFlight

STATUS_SUCCESS = "SUCCESS"
STATUS_PRICE_NOT_FOUND = "PRICE_NOT_FOUND"
STATUS_FLIGHT_NOT_FOUND = "FLIGHT_NOT_FOUND"
STATUS_FLIGHT_NOT_MATCHED = "FLIGHT_NOT_MATCHED"
STATUS_INVALID_RESPONSE = "INVALID_RESPONSE"


@dataclass
class ExtractionResult:
    status: str
    price: Optional[float] = None
    currency: Optional[str] = None
    source: Optional[str] = None
    validated_flight: Optional[str] = None
    seller: Optional[str] = None
    matched_departure_time: Optional[str] = None
    matched_arrival_time: Optional[str] = None
    notes: list = field(default_factory=list)

    # ALL booking_options offers found for this itinerary (not just the
    # cheapest). Each entry: {"seller": str|None, "price": float,
    # "source": str}. `price`/`seller`/`source` above always mirror the
    # cheapest entry of this list - this list is purely additive, for
    # anyone who wants to track every seller's price over time.
    all_offers: list = field(default_factory=list)

    # Secondary/analytical fields, never used to derive `price` above.
    google_lowest_price: Optional[float] = None
    google_price_level: Optional[str] = None
    google_typical_price_low: Optional[float] = None
    google_typical_price_high: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "price": self.price,
            "currency": self.currency,
            "source": self.source,
            "all_offers": self.all_offers,
            "validated_flight": self.validated_flight,
            "seller": self.seller,
            "matched_departure_time": self.matched_departure_time,
            "matched_arrival_time": self.matched_arrival_time,
            "notes": self.notes,
            "google_lowest_price": self.google_lowest_price,
            "google_price_level": self.google_price_level,
            "google_typical_price_low": self.google_typical_price_low,
            "google_typical_price_high": self.google_typical_price_high,
        }


def _parse_hhmm(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def _minutes_apart(a: Optional[datetime], b: Optional[datetime]) -> Optional[int]:
    if a is None or b is None:
        return None
    return int(abs((a - b).total_seconds()) // 60)


def _normalize_flight_number(value: str) -> str:
    return (value or "").replace(" ", "").replace("-", "").upper()


def validate_itinerary(
    selected_flight_entry: dict, expected: ExpectedFlight
) -> tuple[bool, list[str], Optional[str], Optional[str]]:
    """Checks one selected_flights[] entry against the expected flight.

    Returns (is_valid, notes, matched_departure_time, matched_arrival_time).
    """
    notes: list[str] = []
    flights = selected_flight_entry.get("flights") or []

    if len(flights) != 1:
        notes.append(
            f"expected a direct (1-segment) itinerary, got {len(flights)} segment(s)"
        )
        return False, notes, None, None

    segment = flights[0]
    flight_number = _normalize_flight_number(segment.get("flight_number", ""))
    expected_flight_number = _normalize_flight_number(expected.flight_number)
    if flight_number != expected_flight_number:
        notes.append(
            f"flight_number mismatch: got '{segment.get('flight_number')}', "
            f"expected '{expected.flight_number}'"
        )
        return False, notes, None, None

    dep_airport = segment.get("departure_airport") or {}
    arr_airport = segment.get("arrival_airport") or {}

    if dep_airport.get("id") != expected.departure_id:
        notes.append(
            f"departure airport mismatch: got '{dep_airport.get('id')}', "
            f"expected '{expected.departure_id}'"
        )
        return False, notes, None, None

    if arr_airport.get("id") != expected.arrival_id:
        notes.append(
            f"arrival airport mismatch: got '{arr_airport.get('id')}', "
            f"expected '{expected.arrival_id}'"
        )
        return False, notes, None, None

    dep_time_raw = dep_airport.get("time", "")
    if not dep_time_raw.startswith(expected.date):
        notes.append(
            f"date mismatch: departure time '{dep_time_raw}' does not fall on "
            f"'{expected.date}'"
        )
        return False, notes, None, None

    # Optional, best-effort schedule sanity check (approximate published
    # times can differ slightly from what Google returns).
    dep_dt = _parse_hhmm(dep_time_raw)
    arr_dt = _parse_hhmm(arr_airport.get("time", ""))
    expected_dep_dt = _parse_hhmm(f"{expected.date} {expected.departure_time}")
    expected_arr_dt = _parse_hhmm(f"{expected.date} {expected.arrival_time}")

    dep_diff = _minutes_apart(dep_dt, expected_dep_dt)
    if dep_diff is not None and dep_diff > expected.schedule_tolerance_minutes:
        notes.append(
            f"departure time '{dep_time_raw}' is {dep_diff} min from the "
            f"expected ~{expected.departure_time} (tolerance "
            f"{expected.schedule_tolerance_minutes} min) - flagged, not rejected"
        )

    if arr_dt is not None and expected_arr_dt is not None:
        arr_diff = _minutes_apart(arr_dt, expected_arr_dt)
        if arr_diff is not None and arr_diff > expected.schedule_tolerance_minutes:
            notes.append(
                f"arrival time '{arr_airport.get('time')}' is {arr_diff} min from "
                f"the expected ~{expected.arrival_time} - flagged, not rejected"
            )

    return (
        True,
        notes,
        dep_airport.get("time"),
        arr_airport.get("time"),
    )


def _price_candidates_from_booking_options(
    booking_options: list[dict],
) -> list[tuple[float, str, Optional[str], Optional[str]]]:
    """Returns (price, json_source_path, seller, fare_type) tuples, one
    per usable booking_options entry. `fare_type` is booking_options[i]
    .together.option_title when present (e.g. "Basic Economy",
    "Economy Plus") - the same seller can legitimately appear multiple
    times with different fare_type values, which is why "3x Pegasus"
    with different prices is normal, not a bug. Cheapest-first is NOT
    guaranteed here - caller sorts.
    """
    candidates: list[tuple[float, str, Optional[str], Optional[str]]] = []

    for i, option in enumerate(booking_options or []):
        if option.get("separate_tickets"):
            departing = option.get("departing") or {}
            returning = option.get("returning") or {}
            if "price" in departing and "price" in returning:
                price = departing["price"] + returning["price"]
                seller = departing.get("book_with") or returning.get("book_with")
                fare_type = departing.get("option_title") or returning.get("option_title")
                candidates.append(
                    (
                        float(price),
                        f"booking_options[{i}].departing.price + "
                        f"booking_options[{i}].returning.price (separate_tickets)",
                        seller,
                        fare_type,
                    )
                )
                continue
            # fall through to "together" if present even when separate_tickets
            # is true but together also carries a combined price
            together = option.get("together") or {}
            if "price" in together:
                candidates.append(
                    (
                        float(together["price"]),
                        f"booking_options[{i}].together.price",
                        together.get("book_with"),
                        together.get("option_title"),
                    )
                )
            continue

        together = option.get("together") or {}
        if "price" in together:
            candidates.append(
                (
                    float(together["price"]),
                    f"booking_options[{i}].together.price",
                    together.get("book_with"),
                    together.get("option_title"),
                )
            )

    return candidates


def _find_matching_flight_result_price(
    results: dict, expected: ExpectedFlight
) -> Optional[tuple[float, str]]:
    """Defensive fallback: scans best_flights/other_flights (only present
    if SerpApi returns them alongside a pinned search) for an entry whose
    own flights[] match the expected PC2476 itinerary, and returns that
    entry's own top-level "price" field if present.
    """
    for bucket_name in ("best_flights", "other_flights"):
        bucket = results.get(bucket_name) or []
        for i, entry in enumerate(bucket):
            is_valid, _notes, _dep, _arr = validate_itinerary(entry, expected)
            if is_valid and "price" in entry:
                return float(entry["price"]), f"{bucket_name}[{i}].price"
    return None


def extract_price_insights(results: dict) -> dict:
    """Pulls Google's supplementary price_insights fields, kept entirely
    separate from pc2476_price. Never used as a price source above.
    """
    insights = results.get("price_insights") or {}
    typical_range = insights.get("typical_price_range") or [None, None]
    low, high = (typical_range + [None, None])[:2]
    return {
        "google_lowest_price": insights.get("lowest_price"),
        "google_price_level": insights.get("price_level"),
        "google_typical_price_low": low,
        "google_typical_price_high": high,
        "google_price_history": insights.get("price_history"),
    }


def extract_pc2476_price(
    results: dict, expected: ExpectedFlight = None
) -> ExtractionResult:
    """Main entry point. Never raises on malformed input - always returns
    an ExtractionResult with an explicit status.
    """
    from tracker.config import EXPECTED_FLIGHT

    expected = expected or EXPECTED_FLIGHT

    if not isinstance(results, dict):
        return ExtractionResult(
            status=STATUS_INVALID_RESPONSE,
            notes=["results is not a JSON object"],
        )

    insights = extract_price_insights(results)

    if results.get("error"):
        return ExtractionResult(
            status=STATUS_INVALID_RESPONSE,
            notes=[f"SerpApi returned an error field: {results['error']}"],
            **{k: v for k, v in insights.items() if k != "google_price_history"},
        )

    selected_flights = results.get("selected_flights")
    if not selected_flights:
        return ExtractionResult(
            status=STATUS_FLIGHT_NOT_FOUND,
            notes=["'selected_flights' missing or empty in response"],
            **{k: v for k, v in insights.items() if k != "google_price_history"},
        )

    # For a one-way pinned search there should be exactly one entry.
    entry = selected_flights[0]
    is_valid, notes, dep_time, arr_time = validate_itinerary(entry, expected)

    if not is_valid:
        return ExtractionResult(
            status=STATUS_FLIGHT_NOT_MATCHED,
            notes=notes,
            **{k: v for k, v in insights.items() if k != "google_price_history"},
        )

    validated_flight = expected.flight_number

    booking_options = results.get("booking_options") or []
    candidates = _price_candidates_from_booking_options(booking_options)

    if candidates:
        candidates.sort(key=lambda c: c[0])
        price, source, seller, fare_type = candidates[0]
        currency = results.get("search_parameters", {}).get("currency")
        extra_note = (
            f"{len(candidates)} booking_options price(s) found for this "
            f"itinerary; used the minimum"
            if len(candidates) > 1
            else "1 booking_options price found for this itinerary"
        )
        all_offers = [
            {
                "seller": c_seller,
                "price": c_price,
                "source": c_source,
                "fare_type": c_fare_type,
            }
            for c_price, c_source, c_seller, c_fare_type in candidates
        ]
        return ExtractionResult(
            status=STATUS_SUCCESS,
            price=price,
            currency=currency,
            source=source,
            validated_flight=validated_flight,
            seller=seller,
            matched_departure_time=dep_time,
            matched_arrival_time=arr_time,
            notes=notes + [extra_note],
            all_offers=all_offers,
            **{k: v for k, v in insights.items() if k != "google_price_history"},
        )

    # booking_options absent/empty - defensive fallback to a matching
    # entry inside best_flights/other_flights, if SerpApi happened to
    # return those alongside the pinned search.
    fallback = _find_matching_flight_result_price(results, expected)
    if fallback is not None:
        price, source = fallback
        return ExtractionResult(
            status=STATUS_SUCCESS,
            price=price,
            currency=results.get("search_parameters", {}).get("currency"),
            source=source,
            validated_flight=validated_flight,
            matched_departure_time=dep_time,
            matched_arrival_time=arr_time,
            notes=notes + ["price recovered via best_flights/other_flights fallback"],
            **{k: v for k, v in insights.items() if k != "google_price_history"},
        )

    return ExtractionResult(
        status=STATUS_PRICE_NOT_FOUND,
        validated_flight=validated_flight,
        matched_departure_time=dep_time,
        matched_arrival_time=arr_time,
        notes=notes
        + ["itinerary matched but no usable price in booking_options or fallbacks"],
        **{k: v for k, v in insights.items() if k != "google_price_history"},
    )
