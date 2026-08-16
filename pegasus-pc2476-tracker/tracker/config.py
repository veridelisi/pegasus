"""
All fixed configuration for the PC2476 tracker lives here.

Nothing in this file should require an API call to determine - it's the
static description of "what flight am I tracking, and under what limits".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Timezone
# --------------------------------------------------------------------------
ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

# --------------------------------------------------------------------------
# The single flight being tracked
# --------------------------------------------------------------------------
FLIGHT_NUMBER = "PC2476"
DEPARTURE_ID = "ESB"
ARRIVAL_ID = "ADB"
FLIGHT_DATE = "2026-09-17"          # YYYY-MM-DD
EXPECTED_DEPARTURE_TIME = "19:30"   # local time at ESB, HH:MM, approximate
EXPECTED_ARRIVAL_TIME = "20:45"     # local time at ADB, HH:MM, approximate

# How much slack to allow when comparing the approximate published schedule
# above against the departure/arrival times actually returned by Google
# Flights (these can drift by a few minutes release to release).
SCHEDULE_TOLERANCE_MINUTES = 20

CURRENCY = "TRY"
COUNTRY = "tr"   # gl parameter
LANGUAGE = "en"  # hl parameter

# --------------------------------------------------------------------------
# SerpApi search parameters
# --------------------------------------------------------------------------
def build_search_parameters() -> dict:
    """Builds the exact search_parameters dict sent to SerpApi.

    This intentionally mirrors the user's own query, not the generic
    round-trip CDG->AUS documentation example. One-way, single pinned
    segment, no return_date.
    """
    selected_flights_json = json.dumps(
        {
            "outbound": [
                {
                    "flight_number": FLIGHT_NUMBER,
                    "departure_id": DEPARTURE_ID,
                    "arrival_id": ARRIVAL_ID,
                    "date": FLIGHT_DATE,
                }
            ]
        }
    )
    return {
        "engine": "google_flights",
        "hl": LANGUAGE,
        "gl": COUNTRY,
        "type": "2",  # one way
        "departure_id": DEPARTURE_ID,
        "arrival_id": ARRIVAL_ID,
        "outbound_date": FLIGHT_DATE,
        "currency": CURRENCY,
        "selected_flights_json": selected_flights_json,
    }


@dataclass(frozen=True)
class ExpectedFlight:
    flight_number: str = FLIGHT_NUMBER
    departure_id: str = DEPARTURE_ID
    arrival_id: str = ARRIVAL_ID
    date: str = FLIGHT_DATE
    departure_time: str = EXPECTED_DEPARTURE_TIME
    arrival_time: str = EXPECTED_ARRIVAL_TIME
    schedule_tolerance_minutes: int = SCHEDULE_TOLERANCE_MINUTES


EXPECTED_FLIGHT = ExpectedFlight()

# --------------------------------------------------------------------------
# Quota protection
# --------------------------------------------------------------------------
MAX_MONTHLY_CALLS = 245
MIN_SECONDS_BETWEEN_CALLS = int(2.5 * 3600)  # 2h30m duplicate-protection window

# --------------------------------------------------------------------------
# Filesystem layout
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "flight_prices.db"
CSV_PATH = DATA_DIR / "price_history.csv"
RAW_RESPONSES_DIR = DATA_DIR / "raw_responses"
MAX_RAW_RESPONSES_KEPT = 40  # simple retention cap, see save_raw_response()
