"""KST-aware "today" — this dashboard's data refreshes on a KST clock (daily
07:00 KST), so every "as of today" default must use KST, not the host
server's local time. A server running in UTC (as most cloud hosts do)
would otherwise think it's still yesterday until 09:00 UTC (= 18:00 KST),
capping date pickers and cache keys a full day behind KST reality.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def today_kst() -> date:
    return datetime.now(KST).date()
