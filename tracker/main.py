"""
Entry point for the PC2476 price tracker.

Normal scheduled behaviour (no flags): make AT MOST one SerpApi request,
extract and validate the PC2476 price, write one row to SQLite, refresh
the CSV export, and exit. No loops, no polling, no follow-up calls.

Dev/debug modes that spend ZERO extra SerpApi credits beyond what's
explicitly requested:
    --save-response PATH   perform the one normal search, additionally
                            save the complete raw JSON to PATH
    --from-file PATH       load a previously saved JSON file and run
                            extraction against it; makes NO network call
                            and does NOT write to the database or count
                            toward quota (pure dry run / test)

Manual-override flags (for local testing only - do not use in the
scheduled GitHub Actions run):
    --skip-quota-check
    --skip-duplicate-check
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from tracker import db
from tracker.config import (
    CSV_PATH,
    EXPECTED_FLIGHT,
    ISTANBUL_TZ,
    MAX_RAW_RESPONSES_KEPT,
    RAW_RESPONSES_DIR,
    build_search_parameters,
)
from tracker.extract import STATUS_SUCCESS, ExtractionResult, extract_pc2476_price
from tracker.quota import (
    STATUS_MONTHLY_LIMIT_GUARD,
    STATUS_SKIPPED_DUPLICATE,
    STATUS_TRACKING_COMPLETE,
    evaluate_guards,
)
from tracker.serp_client import SerpApiCallError, run_search

STATUS_API_ERROR = "API_ERROR"
STATUS_NETWORK_ERROR = "NETWORK_ERROR"

NON_API_CALL_STATUSES = {
    STATUS_MONTHLY_LIMIT_GUARD,
    STATUS_SKIPPED_DUPLICATE,
    STATUS_TRACKING_COMPLETE,
    STATUS_NETWORK_ERROR,
}


def save_raw_response(results: dict, reason: str) -> Path:
    RAW_RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(ISTANBUL_TZ).strftime("%Y%m%dT%H%M%S")
    safe_reason = reason.lower().replace(" ", "_")[:40]
    path = RAW_RESPONSES_DIR / f"{timestamp}_{safe_reason}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    _prune_raw_responses()
    return path


def _prune_raw_responses() -> None:
    """Keeps repository growth bounded (see requirement #19)."""
    if not RAW_RESPONSES_DIR.exists():
        return
    files = sorted(RAW_RESPONSES_DIR.glob("*.json"), key=lambda p: p.name)
    excess = len(files) - MAX_RAW_RESPONSES_KEPT
    for path in files[:max(excess, 0)]:
        path.unlink(missing_ok=True)


def should_save_raw_evidence(
    result: ExtractionResult, is_first_row: bool, previous_price: float | None
) -> tuple[bool, str]:
    if is_first_row:
        return True, "first_observation"
    if result.status != STATUS_SUCCESS:
        return True, f"extraction_{result.status.lower()}"
    if previous_price is not None and result.price != previous_price:
        return True, "price_changed"
    return False, ""


def write_csv(conn) -> None:
    import csv

    rows = db.export_all(conn)
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "checked_at", "flight_number", "flight_date", "departure_time",
        "pc2476_price", "currency", "google_lowest_price",
        "google_price_level", "status",
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fieldnames})


def run_from_file(path: str) -> int:
    """Zero-API-call dry run against a saved JSON response."""
    with open(path, "r", encoding="utf-8") as f:
        results = json.load(f)
    result = extract_pc2476_price(results, EXPECTED_FLIGHT)
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return 0


def run_normal(save_response_path: str | None, skip_quota_check: bool, skip_duplicate_check: bool) -> int:
    now = datetime.now(ISTANBUL_TZ)

    with db.connect() as conn:
        blocking_status = evaluate_guards(
            conn,
            now=now,
            skip_duplicate_check=skip_duplicate_check,
            skip_quota_check=skip_quota_check,
        )

        if blocking_status:
            is_first_row = db.row_count(conn) == 0
            counted = blocking_status not in NON_API_CALL_STATUSES
            db.insert_observation(
                conn,
                {
                    "checked_at": now.isoformat(timespec="seconds"),
                    "flight_date": EXPECTED_FLIGHT.date,
                    "flight_number": EXPECTED_FLIGHT.flight_number,
                    "status": blocking_status,
                    "counted_toward_quota": int(counted),
                },
            )
            write_csv(conn)
            print(f"Skipped SerpApi call: {blocking_status}")
            return 0

        search_parameters = build_search_parameters()

        try:
            results = run_search(search_parameters)
            api_error_message = results.get("error") if isinstance(results, dict) else None
            network_failed = False
        except SerpApiCallError as exc:
            results = {}
            api_error_message = str(exc)
            network_failed = True

        status_for_quota = STATUS_NETWORK_ERROR if network_failed else "CALL_MADE"
        counted = status_for_quota != STATUS_NETWORK_ERROR

        if network_failed:
            db.insert_observation(
                conn,
                {
                    "checked_at": now.isoformat(timespec="seconds"),
                    "flight_date": EXPECTED_FLIGHT.date,
                    "flight_number": EXPECTED_FLIGHT.flight_number,
                    "status": STATUS_NETWORK_ERROR,
                    "error_message": api_error_message,
                    "counted_toward_quota": 0,
                },
            )
            write_csv(conn)
            print(f"NETWORK_ERROR: {api_error_message}", file=sys.stderr)
            return 1

        if save_response_path:
            with open(save_response_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"Saved raw response to {save_response_path}")

        if api_error_message:
            monthly_calls = db.count_calls_this_month(conn, now.year, now.month) + 1
            db.insert_observation(
                conn,
                {
                    "checked_at": now.isoformat(timespec="seconds"),
                    "flight_date": EXPECTED_FLIGHT.date,
                    "flight_number": EXPECTED_FLIGHT.flight_number,
                    "status": STATUS_API_ERROR,
                    "error_message": str(api_error_message),
                    "monthly_call_number": monthly_calls,
                    "counted_toward_quota": 1,
                },
            )
            write_csv(conn)
            save_raw_response(results, "api_error")
            print(f"API_ERROR: {api_error_message}", file=sys.stderr)
            return 1

        result = extract_pc2476_price(results, EXPECTED_FLIGHT)

        previous_success = db.get_latest_success(conn)
        previous_price = previous_success["pc2476_price"] if previous_success else None
        is_first_row = db.row_count(conn) == 0

        monthly_calls = db.count_calls_this_month(conn, now.year, now.month) + 1
        checked_at_str = now.isoformat(timespec="seconds")

        observation_id = db.insert_observation(
            conn,
            {
                "checked_at": checked_at_str,
                "flight_date": EXPECTED_FLIGHT.date,
                "flight_number": EXPECTED_FLIGHT.flight_number,
                "departure_airport": EXPECTED_FLIGHT.departure_id,
                "arrival_airport": EXPECTED_FLIGHT.arrival_id,
                "departure_time": result.matched_departure_time,
                "arrival_time": result.matched_arrival_time,
                "pc2476_price": result.price,
                "currency": result.currency,
                "google_lowest_price": result.google_lowest_price,
                "google_price_level": result.google_price_level,
                "google_typical_price_low": result.google_typical_price_low,
                "google_typical_price_high": result.google_typical_price_high,
                "status": result.status,
                "error_message": "; ".join(result.notes) if result.notes else None,
                "price_source": result.source,
                "seller": result.seller,
                "serpapi_search_id": (results.get("search_metadata") or {}).get("id"),
                "monthly_call_number": monthly_calls,
                "counted_toward_quota": 1,
            },
        )

        # Every seller's offer for this itinerary, not just the cheapest
        # one stored above - lets you track individual sellers over time.
        if result.all_offers:
            db.insert_seller_offers(
                conn, observation_id, checked_at_str, result.currency, result.all_offers
            )

        write_csv(conn)

        save_evidence, reason = should_save_raw_evidence(result, is_first_row, previous_price)
        if save_evidence and not save_response_path:
            path = save_raw_response(results, reason)
            print(f"Saved raw evidence ({reason}) to {path}")

        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return 0 if result.status == STATUS_SUCCESS else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pegasus PC2476 price tracker")
    parser.add_argument(
        "--save-response",
        metavar="PATH",
        help="Perform the normal single search AND save the full raw JSON to PATH.",
    )
    parser.add_argument(
        "--from-file",
        metavar="PATH",
        help="Zero-API-call dry run: extract a price from a previously saved JSON file.",
    )
    parser.add_argument(
        "--skip-quota-check",
        action="store_true",
        help="Local testing only: bypass the monthly quota guard.",
    )
    parser.add_argument(
        "--skip-duplicate-check",
        action="store_true",
        help="Local testing only: bypass the 2h30m duplicate-protection guard.",
    )
    args = parser.parse_args(argv)

    if args.from_file:
        return run_from_file(args.from_file)

    return run_normal(
        save_response_path=args.save_response,
        skip_quota_check=args.skip_quota_check,
        skip_duplicate_check=args.skip_duplicate_check,
    )


if __name__ == "__main__":
    raise SystemExit(main())
