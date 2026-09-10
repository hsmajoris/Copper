"""Local CSV accumulation for COMEX copper warehouse stock (manual-entry
only — see data_sources.py module docstring for why this indicator has no
auto-scraper: CME's own delivery-report endpoint explicitly prohibits
automated access under its Data Terms of Use). Every value saved here is
timestamped, so once ~1 year of history has accumulated organically it can
be reconsidered for the backtest (see config.REALTIME_ONLY_INDICATORS and
README.md Phase 2 notes).
"""

from datetime import date
from pathlib import Path

import pandas as pd

COMEX_HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "comex_stock_history.csv"
COLUMNS = ["date", "tons", "recorded_at"]


def load_comex_history() -> pd.DataFrame:
    if not COMEX_HISTORY_PATH.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(COMEX_HISTORY_PATH, dtype={"date": str})
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[COLUMNS]


def save_comex_value(stock_date: date, tons: float) -> None:
    """Upsert by `stock_date` — re-saving the same report date overwrites
    its value rather than duplicating a row.
    """
    df = load_comex_history()
    date_iso = stock_date.isoformat()
    mask = df["date"] == date_iso
    new_row = {"date": date_iso, "tons": tons, "recorded_at": date.today().isoformat()}
    if mask.any():
        df.loc[mask, list(new_row.keys())] = list(new_row.values())
    else:
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    df = df.sort_values("date").reset_index(drop=True)
    COMEX_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(COMEX_HISTORY_PATH, index=False)


def latest_comex() -> dict | None:
    df = load_comex_history()
    if df.empty:
        return None
    row = df.sort_values("date").iloc[-1]
    return {"date": row["date"], "tons": float(row["tons"]), "recorded_at": row["recorded_at"]}


def first_collection_date() -> str | None:
    df = load_comex_history()
    if df.empty:
        return None
    return df.sort_values("date").iloc[0]["date"]


def days_of_history() -> int:
    df = load_comex_history()
    if df.empty:
        return 0
    first = date.fromisoformat(df.sort_values("date").iloc[0]["date"])
    return (date.today() - first).days
