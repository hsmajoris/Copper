"""Local CSV accumulation for the two semi-automated PMI indicators (China,
US ISM). Every value a user saves via the "PMI 새로고침" UI — whether it
came from a successful scrape or was typed in by hand after a failed scrape
— is appended here with the date it was saved. This accumulating record is
also this dashboard's only source of PMI history: there is no free official
historical archive (see config.py FOOTNOTES), so over time this file
becomes the backtest's PMI input (see README.md).
"""

from datetime import date
from pathlib import Path

import pandas as pd

from . import config

PMI_HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "pmi_history.csv"
COLUMNS = ["indicator", "period", "value", "recorded_at", "source"]


def load_pmi_history() -> pd.DataFrame:
    if not PMI_HISTORY_PATH.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(PMI_HISTORY_PATH, dtype={"period": str})
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[COLUMNS]


def save_pmi_value(indicator: str, period: str, value: float, source: str, recorded_at: date | None = None) -> None:
    """Upsert one (indicator, period) row — re-saving the same month
    overwrites its value/source/recorded_at rather than duplicating a row,
    so correcting a mis-typed manual entry doesn't leave stale duplicates
    behind to confuse the streak/backtest calculations.
    """
    recorded = (recorded_at or date.today()).isoformat()
    df = load_pmi_history()
    mask = (df["indicator"] == indicator) & (df["period"] == period)
    new_row = {
        "indicator": indicator,
        "period": period,
        "value": value,
        "recorded_at": recorded,
        "source": source,
    }
    if mask.any():
        df.loc[mask, list(new_row.keys())] = list(new_row.values())
    else:
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    df = df.sort_values(["indicator", "period"]).reset_index(drop=True)
    PMI_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(PMI_HISTORY_PATH, index=False)


def latest_pmi(indicator: str) -> dict | None:
    df = load_pmi_history()
    rows = df[df["indicator"] == indicator].sort_values("period")
    if rows.empty:
        return None
    row = rows.iloc[-1]
    return {
        "period": row["period"],
        "value": float(row["value"]),
        "recorded_at": row["recorded_at"],
        "source": row["source"],
    }


def history_for_backtest(indicator: str) -> pd.DataFrame:
    """Chronological (period, value) rows for one PMI indicator, period as a
    month-start Timestamp index — the shape backtest.py needs to align PMI
    against the daily price series (forward-filled within each month).
    """
    df = load_pmi_history()
    rows = df[df["indicator"] == indicator].sort_values("period").copy()
    if rows.empty:
        return pd.DataFrame(columns=["value"])
    rows["period_start"] = pd.to_datetime(rows["period"], format="%Y-%m")
    return rows.set_index("period_start")[["value"]].astype(float)


def days_since_recorded(recorded_at: str, as_of: date) -> int:
    recorded_date = date.fromisoformat(recorded_at)
    return (as_of - recorded_date).days


def is_stale(recorded_at: str, as_of: date) -> bool:
    return days_since_recorded(recorded_at, as_of) > config.PMI_STALENESS_DAYS
