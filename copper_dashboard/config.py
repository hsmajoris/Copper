"""Static configuration for the copper correlation dashboard: indicator
metadata, fixed reference text (structure/meaning/correlation direction),
and footnotes.

This is a direct structural port of the Gold dashboard's config.py — see
COPPER_TRADING_LOGIC.md 9장 for the full list of deliberate differences.
The short version: real_rate (FRED DFII10) has no meaningful copper analog
and is dropped entirely; DXY is kept (same ticker, same "inverse"
direction); a new indicator, FXI (iShares China Large-Cap ETF, a market-
price proxy for Chinese industrial demand — copper's largest end market)
replaces it as green_count's second leg, with the OPPOSITE correlation
direction from DXY ("positive", not "inverse") — this is the one place the
mechanical green_count computation itself needs a per-indicator direction
switch, which config.CORRELATION_DIRECTION already provides.
"""

from datetime import date

INDICATOR_ORDER = ["dxy", "fxi"]

# Calendar-day (역일) windows, not trading-day counts — metrics.compute_sma
# averages every observation within the trailing N calendar days, whatever
# number of trading days that happens to contain. 7/90 stay the 일주일(week)/
# 세달(quarter) units used throughout this project; the middle window was
# changed from 30(한달) to 20 calendar days at the user's direct request
# (2026-09) — COPPER_TRADING_LOGIC.md 9장 참고.
MA_WINDOWS = [90, 20, 7]

# Indicators that actually feed a real buy/sell trigger (the dxy+fxi
# green_count condition). Both of copper's indicators are signal
# indicators — unlike Gold (which has WTI/VIX as reference-only display
# indicators alongside real_rate/dxy's green_count pair), this project
# doesn't carry any reference-only indicator, since none was requested.
GREEN_COUNT_SIGNAL_INDICATORS = {"dxy", "fxi"}

# Which price series the backtest ("유효성 검증") page's entire pipeline uses
# for copper itself — a single shared session_state key so the choice is one
# piece of state no matter which page renders the control. "intl" (default)
# is HG=F (COMEX copper futures), the direct analog of Gold's GC=F. "krx"
# swaps in KODEX 구리선물(H) (138910) — there is no KRX copper SPOT market
# (unlike gold's real KRX 금현물 04020000), so this is a listed, KRW-
# denominated, currency-hedged ETF that actually tracks a COMEX-linked
# index (S&P GSCI North American Copper Index) rather than a real domestic
# spot quote. See COPPER_TRADING_LOGIC.md 9장 for why this ETF (not TIGER
# 구리실물, which is LME-based and currency-UNHEDGED) was chosen: matching
# the international benchmark's own COMEX basis, and avoiding a double
# exposure to dollar direction on top of the DXY signal itself.
COPPER_PRICE_BASIS_INTL = "intl"
COPPER_PRICE_BASIS_KRX = "krx"
COPPER_PRICE_BASIS_STATE_KEY = "copper_price_basis"
COPPER_PRICE_BASIS_LABELS = {
    COPPER_PRICE_BASIS_INTL: "① 국제 구리 시세 (USD/lb, HG=F)",
    COPPER_PRICE_BASIS_KRX: "② KODEX 구리선물(H) (KRW, 국내 상장 ETF)",
}
COPPER_PRICE_BASIS_DEFAULT = COPPER_PRICE_BASIS_KRX

# KODEX 구리선물(H) (138910) listing date — no earlier data exists at the
# source, so any analysis window under COPPER_PRICE_BASIS_KRX is clamped to
# not start before it (see timeseries.copper_window_would_clamp_to_krx).
KRX_COPPER_ETF_TICKER = "138910"
KRX_COPPER_ETF_EARLIEST_DATE = date(2011, 3, 15)

INDICATOR_META = {
    "dxy": {
        "label": "달러인덱스",
        "source": "Yahoo Finance (DX-Y.NYB)",
        "unit": "",
        "decimals": 2,
    },
    "fxi": {
        "label": "FXI (iShares China Large-Cap ETF)",
        "source": "Yahoo Finance (FXI)",
        "unit": "$",
        "decimals": 2,
    },
}

STATIC_ROWS = {
    "구조": {
        "dxy": "6개국 통화 바스켓 내 달러 비율",
        "fxi": "중국 대형주 50종목 추종 ETF (뉴욕 상장, USD)",
    },
    "의미": {
        "dxy": "달러 표시 원자재인 구리의 역내 구매력에 영향 — 달러 강세 시 비달러권의 실물 수요가 위축되는 경로로 작용함",
        "fxi": (
            "세계 최대 구리 소비국인 중국의 경기·산업 활동에 대한 시장의 실시간 평가를 담은 "
            "가격 기반 대리지표 — 중국 대형주(다수가 금융·산업·에너지 섹터)의 주가가 오른다는 "
            "것은 시장이 중국의 경기 확장(=구리 수요 확대)을 반영하고 있다는 신호로 해석"
        ),
    },
    "상관관계 방향": {
        "dxy": "역상관 (구조적 음(-)의 상관)",
        "fxi": "정상관 (중국 경기 프록시 — 금 프로젝트의 실질금리·DXY와 달리 방향이 반대(positive)인 지표)",
    },
}

ROW_ORDER = ["구조", "의미", "상관관계 방향"]

# Machine-readable version of the "상관관계 방향" row above, used to decide what
# counts as "copper-friendly" for MA-row highlighting:
# - "inverse": indicator falling below its MA is copper-friendly (DXY)
# - "positive": indicator rising above its MA is copper-friendly (FXI)
# This is the one constant that actually encodes the "금과 다른 핵심 지점"
# the task brief called out: DXY keeps Gold's own "inverse" direction
# unchanged, while FXI is deliberately configured "positive" — the exact
# opposite of Gold's second green_count leg (real_rate, "inverse"). Nothing
# in signals.py/backtest.py needs to know this — they read it from here.
CORRELATION_DIRECTION = {
    "dxy": "inverse",
    "fxi": "positive",
}

FOOTNOTES = {
    "dxy": (
        "업계 자료 기준 상관계수 약 -0.65~-0.82 수준으로 보고되나 학술 피어리뷰로 확정된 수치는 "
        "아님. 이 프로젝트 자체 검증(2005-01~2026-09, HG=F 일간수익률 기준, 2년 단위 11개 "
        "구간): 전 구간에서 방향이 한 번도 바뀌지 않고 R²(설명력) 0.021~0.230 사이에서 일관되게 "
        "역상관 (COPPER_TRADING_LOGIC.md 11장 참고)."
    ),
    "fxi": (
        "중국 제조업 PMI·산업생산 같은 공식 통계는 발표 주기가 월 단위로 느리고 개정(수정 발표)이 "
        "잦아, 이 프로젝트는 그 대신 매일 갱신되는 시장가격 기반 대리지표(FXI)를 채택함 — "
        "장단점은 COPPER_TRADING_LOGIC.md 9·10장 참고. 이 프로젝트 자체 검증(2005-01~2026-09, "
        "일간수익률 기준, 2년 단위 11개 구간): 전 구간에서 방향이 한 번도 바뀌지 않고 "
        "R² 0.035~0.355 사이에서 일관되게 정상관. 원래 CHII(Global X MSCI China Industrials "
        "ETF)를 검토했으나 상장폐지에 가까운 상태(최근 데이터 사실상 없음)라 FXI로 대체함."
    ),
}
