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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
