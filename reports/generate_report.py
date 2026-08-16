"""
Generates:
  1. data/price_history.csv  (also refreshed after every observation by
     tracker/main.py - this script re-derives it from SQLite so it can
     be re-run any time without touching the API)
  2. data/pc2476_price_chart.png - a chart of ONLY pc2476_price over time,
     with an optional, clearly-labeled second series for Google's
     lowest available route price. Never plots price_insights as if it
     were the PC2476 price.

Run with:  python -m reports.generate_report
Makes ZERO SerpApi requests.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import db  # noqa: E402
from tracker.config import CSV_PATH, DATA_DIR  # noqa: E402

CHART_PATH = DATA_DIR / "pc2476_price_chart.png"
SELLER_CSV_PATH = DATA_DIR / "seller_prices.csv"
SELLER_CHART_PATH = DATA_DIR / "seller_price_chart.png"


def write_full_csv(conn) -> None:
    rows = db.export_all(conn)
    fieldnames = [
        "checked_at", "flight_number", "flight_date", "departure_time",
        "pc2476_price", "currency", "google_lowest_price",
        "google_price_level", "status",
    ]
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fieldnames})
    print(f"Wrote {len(rows)} rows to {CSV_PATH}")


def write_seller_csv(conn) -> None:
    """One row per (observation, seller, fare_type) offer - the raw
    material for tracking each individual seller/fare combination's
    PC2476 price over time. The same seller can appear multiple times
    per checked_at with different fare_type values (e.g. Pegasus
    "Basic Economy" vs "Economy Plus") - that's expected, not a bug.
    """
    rows = db.export_seller_offers(conn)
    fieldnames = ["checked_at", "seller", "fare_type", "price", "currency", "source"]
    SELLER_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SELLER_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fieldnames})
    print(f"Wrote {len(rows)} seller-offer rows to {SELLER_CSV_PATH}")


def plot_seller_chart(conn) -> None:
    """Plots ONE line per seller: the CHEAPEST fare_type that seller had
    at each observation. Multiple fare classes per seller (see
    write_seller_csv) are real data and stay in the CSV, but plotting
    all of them here would draw several points at the exact same
    timestamp for one seller, which reads as a confusing zigzag rather
    than a trend. Aggregating to "cheapest per seller per observation"
    keeps the chart readable while the CSV keeps full fare-class detail.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed - skipping seller chart.")
        return

    from collections import defaultdict
    from datetime import datetime

    rows = db.export_seller_offers(conn)
    if not rows:
        print("No seller offers recorded yet - skipping seller chart.")
        return

    # (seller, checked_at) -> cheapest price seen for that seller at
    # that observation, collapsing multiple fare_type rows into one point.
    cheapest_per_seller_per_time: dict[tuple[str, str], float] = {}
    currency = ""
    for row in rows:
        seller = row["seller"] or "Unknown seller"
        key = (seller, row["checked_at"])
        price = row["price"]
        if price is None:
            continue
        if key not in cheapest_per_seller_per_time or price < cheapest_per_seller_per_time[key]:
            cheapest_per_seller_per_time[key] = price
        currency = row["currency"] or currency

    by_seller: dict[str, list[tuple]] = defaultdict(list)
    for (seller, checked_at), price in cheapest_per_seller_per_time.items():
        by_seller[seller].append((datetime.fromisoformat(checked_at), price))

    fig, ax = plt.subplots(figsize=(11, 5.5))
    for seller, points in sorted(by_seller.items()):
        points.sort(key=lambda p: p[0])
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        ax.plot(xs, ys, marker="o", markersize=3, linewidth=1.3, label=seller)

    ax.set_title(
        "Pegasus PC2476 - Cheapest Fare per Seller (ESB \u2192 ADB, 2026-09-17)"
    )
    ax.set_xlabel("Observation timestamp (Europe/Istanbul)")
    ax.set_ylabel(f"Price ({currency})" if currency else "Price")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(SELLER_CHART_PATH, dpi=150)
    print(f"Wrote seller chart to {SELLER_CHART_PATH}")


def plot_chart(conn) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed - skipping chart. `pip install matplotlib` to enable.")
        return

    from datetime import datetime

    rows = db.export_all(conn)
    success_rows = [r for r in rows if r["status"] == "SUCCESS" and r["pc2476_price"] is not None]

    if not success_rows:
        print("No SUCCESS observations yet - skipping chart.")
        return

    timestamps = [datetime.fromisoformat(r["checked_at"]) for r in success_rows]
    pc2476_prices = [r["pc2476_price"] for r in success_rows]
    google_prices = [r["google_lowest_price"] for r in success_rows]
    currency = success_rows[-1]["currency"] or ""

    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(
        timestamps, pc2476_prices,
        marker="o", linewidth=2, color="#c8102e",
        label="Pegasus PC2476 observed price (this project's own data)",
    )

    if any(p is not None for p in google_prices):
        ax.plot(
            timestamps, google_prices,
            marker=".", linewidth=1, linestyle="--", color="#888888",
            label="Google Flights lowest available price (route, not necessarily PC2476)",
        )

    ax.set_title("Pegasus PC2476 Price History (ESB \u2192 ADB, 2026-09-17)")
    ax.set_xlabel("Observation timestamp (Europe/Istanbul)")
    ax.set_ylabel(f"Price ({currency})" if currency else "Price")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(CHART_PATH, dpi=150)
    print(f"Wrote chart to {CHART_PATH}")


def main() -> int:
    with db.connect() as conn:
        write_full_csv(conn)
        plot_chart(conn)
        write_seller_csv(conn)
        plot_seller_chart(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
