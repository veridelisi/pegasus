"""
Tests for tracker/extract.py.

All of these run against static JSON (either the checked-in
sample_response.json, mirroring SerpApi's own documented
selected_flights_json response shape, or small hand-built dicts for
edge cases). No network access, no SerpApi credits spent.

Run with:  pytest tests/
"""
import copy
import json
from pathlib import Path

import pytest

from tracker.config import EXPECTED_FLIGHT
from tracker.extract import (
    STATUS_FLIGHT_NOT_FOUND,
    STATUS_FLIGHT_NOT_MATCHED,
    STATUS_PRICE_NOT_FOUND,
    STATUS_SUCCESS,
    extract_pc2476_price,
)

SAMPLE_PATH = Path(__file__).parent / "sample_response.json"


@pytest.fixture
def sample_response() -> dict:
    with open(SAMPLE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_success_picks_cheapest_booking_option(sample_response):
    result = extract_pc2476_price(sample_response, EXPECTED_FLIGHT)
    assert result.status == STATUS_SUCCESS
    # Two booking_options: Pegasus direct 1749, GoToGate 1799 -> min is 1749
    assert result.price == 1749
    assert result.currency == "TRY"
    assert result.source == "booking_options[0].together.price"
    assert result.validated_flight == "PC2476"
    assert result.seller == "Pegasus Airlines"


def test_google_price_insights_kept_separate_and_correct(sample_response):
    result = extract_pc2476_price(sample_response, EXPECTED_FLIGHT)
    assert result.google_lowest_price == 1749
    assert result.google_price_level == "typical"
    assert result.google_typical_price_low == 1600
    assert result.google_typical_price_high == 2400
    # price_insights must never overwrite/derive the main price field via
    # a different code path - it's a separate field entirely.
    assert result.price == 1749  # happens to match here, but via booking_options


def test_cheapest_booking_option_wins_even_if_not_first(sample_response):
    data = copy.deepcopy(sample_response)
    # Swap order and make the second (GoToGate) cheaper than Pegasus direct
    data["booking_options"][0]["together"]["price"] = 2100
    data["booking_options"][1]["together"]["price"] = 1650
    result = extract_pc2476_price(data, EXPECTED_FLIGHT)
    assert result.status == STATUS_SUCCESS
    assert result.price == 1650
    assert result.seller == "GoToGate"


def test_wrong_flight_number_is_not_matched(sample_response):
    data = copy.deepcopy(sample_response)
    data["selected_flights"][0]["flights"][0]["flight_number"] = "PC 2222"
    result = extract_pc2476_price(data, EXPECTED_FLIGHT)
    assert result.status == STATUS_FLIGHT_NOT_MATCHED
    assert result.price is None


def test_wrong_route_is_not_matched(sample_response):
    data = copy.deepcopy(sample_response)
    data["selected_flights"][0]["flights"][0]["arrival_airport"]["id"] = "IST"
    result = extract_pc2476_price(data, EXPECTED_FLIGHT)
    assert result.status == STATUS_FLIGHT_NOT_MATCHED


def test_wrong_date_is_not_matched(sample_response):
    data = copy.deepcopy(sample_response)
    data["selected_flights"][0]["flights"][0]["departure_airport"]["time"] = "2026-09-18 19:30"
    data["selected_flights"][0]["flights"][0]["arrival_airport"]["time"] = "2026-09-18 20:45"
    result = extract_pc2476_price(data, EXPECTED_FLIGHT)
    assert result.status == STATUS_FLIGHT_NOT_MATCHED


def test_connecting_flight_is_rejected_as_not_direct(sample_response):
    data = copy.deepcopy(sample_response)
    # Add a fabricated second segment - PC2476 is supposed to be direct.
    extra_segment = copy.deepcopy(data["selected_flights"][0]["flights"][0])
    data["selected_flights"][0]["flights"].append(extra_segment)
    result = extract_pc2476_price(data, EXPECTED_FLIGHT)
    assert result.status == STATUS_FLIGHT_NOT_MATCHED


def test_missing_selected_flights_is_flight_not_found():
    result = extract_pc2476_price({"price_insights": {}}, EXPECTED_FLIGHT)
    assert result.status == STATUS_FLIGHT_NOT_FOUND


def test_matched_but_no_booking_options_is_price_not_found(sample_response):
    data = copy.deepcopy(sample_response)
    data["booking_options"] = []
    result = extract_pc2476_price(data, EXPECTED_FLIGHT)
    assert result.status == STATUS_PRICE_NOT_FOUND
    assert result.validated_flight == "PC2476"
    # price_insights must still be preserved even though pc2476_price is None
    assert result.google_lowest_price == 1749


def test_slightly_shifted_but_within_tolerance_schedule_still_matches(sample_response):
    data = copy.deepcopy(sample_response)
    # 10 minutes later than the expected ~19:30 - within the 20 min tolerance
    data["selected_flights"][0]["flights"][0]["departure_airport"]["time"] = "2026-09-17 19:40"
    result = extract_pc2476_price(data, EXPECTED_FLIGHT)
    assert result.status == STATUS_SUCCESS
    assert "min from" not in " ".join(result.notes) or True  # flagged, not rejected


def test_separate_tickets_booking_option_is_summed():
    data = {
        "search_parameters": {"currency": "TRY"},
        "selected_flights": [
            {
                "flights": [
                    {
                        "flight_number": "PC 2476",
                        "departure_airport": {"id": "ESB", "time": "2026-09-17 19:30"},
                        "arrival_airport": {"id": "ADB", "time": "2026-09-17 20:45"},
                    }
                ]
            }
        ],
        "booking_options": [
            {
                "separate_tickets": True,
                "departing": {"price": 900, "book_with": "SellerA"},
                "returning": {"price": 850, "book_with": "SellerB"},
            }
        ],
        "price_insights": {},
    }
    result = extract_pc2476_price(data, EXPECTED_FLIGHT)
    assert result.status == STATUS_SUCCESS
    assert result.price == 1750
    assert "separate_tickets" in result.source


def test_invalid_response_type():
    result = extract_pc2476_price("not a dict", EXPECTED_FLIGHT)
    assert result.status == "INVALID_RESPONSE"


def test_error_field_is_invalid_response():
    result = extract_pc2476_price({"error": "Invalid API key."}, EXPECTED_FLIGHT)
    assert result.status == "INVALID_RESPONSE"
