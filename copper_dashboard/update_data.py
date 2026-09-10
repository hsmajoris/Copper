"""CLI entrypoint run by the daily GitHub Actions workflow: fetches fresh
daily-indicator data (DXY/WTI/gold-copper ratio) and writes
data/latest.json for the Streamlit app to read. PMI and COMEX are NOT
touched here — they're saved independently through the app's own
button-refresh + manual-entry UI (see pmi_store.py/comex_store.py), so this
script only ever updates the daily-frequency portion of the payload.
"""

import json
from pathlib import Path

from copper_dashboard.build_table import build

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "latest.json"


def main() -> None:
    payload = build()
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {DATA_PATH}")


if __name__ == "__main__":
    main()
