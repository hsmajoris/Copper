"""Static configuration for the copper correlation dashboard: indicator
metadata, weights/tiers for the weighted `copper_friendly` score, fixed
reference text (structure/meaning/correlation direction), and footnotes.

Copper is an industrially/cyclically driven metal, not a safe-haven asset
like gold — its price co-moves with global manufacturing demand rather than
risk-aversion sentiment. So this config deliberately does NOT carry a VIX-
or real-rate-style "safe haven psychology" indicator; instead PMI (China +
US manufacturing) anchors the demand side, alongside DXY, WTI and the
gold/copper ratio ("Dr. Copper" phase indicator).

Every weight/threshold constant below is a starting point, not a verified
optimum — see README.md "가중치·임계값 산출 근거" for how each was derived
from real historical data, and pages/1_백테스트.py for how to re-check them
against out-of-sample performance. They live here, as plain module-level
constants, specifically so they can be tuned in one place without touching
any calculation code.
"""

# ---------------------------------------------------------------------------
# Daily, SMA-tracked indicators shown in the main breakout table (mirrors the
# Gold dashboard's table: header / static rows / 60·30·5-day MA rows / close).
# PMI (monthly) and COMEX stock (real-time-only) are NOT part of this table —
# they get their own cards, since a monthly step series and a daily series
# don't share a breakout-streak concept (see MONTHLY_INDICATOR_ORDER below).
# ---------------------------------------------------------------------------
INDICATOR_ORDER = ["dxy", "gold_copper_ratio", "wti"]

MA_WINDOWS = [60, 30, 5]

# Monthly, step-function indicators (semi-automated: button-scrape + manual
# fallback). No MA concept — "breakout streak" here means "consecutive
# favorable months" instead of "consecutive days above/below an MA" (see
# metrics.favorable_month_streak).
MONTHLY_INDICATOR_ORDER = ["china_pmi", "us_pmi"]

# Real-time-only indicator: shown as an info card, excluded from both the
# copper_friendly score and the backtest (see WEIGHTS/BACKTEST_INDICATORS
# below) until enough history has accumulated organically (no free
# historical archive exists — see README.md).
REALTIME_ONLY_INDICATORS = ["comex_copper_stock"]

# All indicators that participate in the weighted copper_friendly score,
# in display order (main SMA table indicators first, then monthly PMI).
SCORE_INDICATOR_ORDER = ["dxy", "gold_copper_ratio", "wti", "china_pmi", "us_pmi"]

# ---------------------------------------------------------------------------
# copper_friendly weighted score (see signals.py for the scoring function).
#
# Each indicator contributes direction * weight to a raw score, where
# direction in {-1, 0, +1} reflects whether that indicator currently favors
# copper (see CORRELATION_DIRECTION / signals.py for how direction is read
# off each indicator type). The raw score is normalized to 0-100 (50 =
# neutral) using MAX_RAW_SCORE below. If an indicator is stale (PMI not
# refreshed within PMI_STALENESS_DAYS) it is dropped from both the
# numerator and MAX_RAW_SCORE for that computation, so the remaining
# indicators are reweighted rather than silently penalized.
#
# Weight rationale (initial draft — NOT yet re-verified by backtest, per the
# task brief's explicit caveat that whoever set these lacks a feel for
# copper's price behavior):
#   - DXY (1.0): most consistently reported inverse correlation in the
#     metals-macro literature (see FOOTNOTES).
#   - gold/copper ratio (0.8): a widely used phase/regime indicator
#     ("Dr. Copper" vs. gold as a growth-vs-safety barometer).
#   - china_pmi / us_pmi (0.7 each): PMI is demand-side and China is the
#     largest copper consumer, but the literature also documents this
#     correlation collapsing to ~0 during the 2022 shock before re-
#     strengthening — hence a middling, not top, weight.
#   - WTI (0.4): explicitly the weakest/lowest-confidence link (shared
#     commodity-inflation-expectations channel, not a direct copper driver).
WEIGHTS = {
    "dxy": 1.0,
    "gold_copper_ratio": 0.8,
    "china_pmi": 0.7,
    "us_pmi": 0.7,
    "wti": 0.4,
}
MAX_RAW_SCORE = sum(WEIGHTS.values())  # 3.6 — every indicator at +1

# 0-100 score at/above this is treated as "copper-friendly" in the UI badge.
# Kept as a principled, symmetric-around-neutral (50) default rather than
# reverse-fit to any single backtest run — a preliminary 10-year check of
# just the 3 daily indicators (DXY/WTI/ratio; PMI has no historical series
# yet — see backtest.py) actually showed forward 60-day copper returns
# WEAKER when the score was high than when it was low, i.e. mean-reversion,
# not momentum, in that sample. Optimizing this cutoff against that one
# noisy, PMI-less run would be exactly the overfitting the task brief warns
# about. pages/1_백테스트.py's score-distribution histogram is the intended
# tool for revisiting this once PMI history has accumulated — see
# README.md "가중치·임계값 산출 근거" for the full finding.
SCORE_BUY_FRIENDLY_CUTOFF = 60.0
SCORE_SELL_UNFRIENDLY_CUTOFF = 40.0

# A PMI value not refreshed within this many days is excluded from the score
# (and flagged "데이터 오래됨" in the UI) rather than silently kept stale.
PMI_STALENESS_DAYS = 45

# ---------------------------------------------------------------------------
# gold/copper ratio ("Dr. Copper") phase thresholds. Unlike Gold's
# gold/silver ratio (where a HIGH ratio is the bullish-gold signal), a HIGH
# gold/copper ratio means gold is expensive relative to copper — a classic
# recession/risk-off signature (weak industrial demand) — so a LOW ratio is
# the copper-friendly side here.
#
# GC=F is quoted per troy ounce and HG=F per pound, so the raw ratio sits in
# the hundreds (not a small multiple like gold/silver's ~50-100) — these two
# thresholds are the 25th/75th percentiles of the actual 2016-2026 GC=F/HG=F
# daily ratio (calibrated from real 10-year history; see README.md
# "가중치·임계값 산출 근거"), analogous to how Gold's 100/60 pair was chosen
# relative to the academic 80 reference value.
DEFAULT_GC_RATIO_BUY_THRESHOLD = 440.0   # ratio <= this => copper-friendly (favorable phase)
DEFAULT_GC_RATIO_SELL_THRESHOLD = 620.0  # ratio >= this => copper-unfriendly (risk-off phase)

INDICATOR_META = {
    "dxy": {
        "label": "달러인덱스",
        "source": "Yahoo Finance (DX-Y.NYB)",
        "unit": "",
        "decimals": 2,
    },
    "gold_copper_ratio": {
        "label": "금/구리 비율 (Dr. Copper)",
        "source": "yfinance GC=F / HG=F",
        "unit": "",
        "decimals": 1,
    },
    "wti": {
        "label": "WTI 유가",
        "source": "yfinance CL=F",
        "unit": "$",
        "decimals": 2,
    },
    "china_pmi": {
        "label": "중국 제조업 PMI",
        "source": "공식 발표(NBS/Caixin) 버튼 갱신 + 수기 입력",
        "unit": "",
        "decimals": 1,
    },
    "us_pmi": {
        "label": "미국 ISM 제조업 PMI",
        "source": "ISM 공식 발표 버튼 갱신 + 수기 입력",
        "unit": "",
        "decimals": 1,
    },
    "comex_copper_stock": {
        "label": "COMEX 구리 재고",
        "source": "CME Group Copper Stocks",
        "unit": "톤",
        "decimals": 0,
    },
}

STATIC_ROWS = {
    "구조": {
        "dxy": "6개국 통화 바스켓 내 달러 비율",
        "gold_copper_ratio": "금가격 / 구리가격 비율",
        "wti": "WTI 가격",
    },
    "의미": {
        "dxy": "달러 표시 원자재인 구리의 역내 구매력에 영향 — 달러 강세 시 비달러권의 실물 수요가 위축되는 경로로 작용함",
        "gold_copper_ratio": (
            "금은 안전자산, 구리는 경기민감 산업금속이라는 상반된 성격을 이용한 국면판단 보조지표 "
            "('Dr. Copper') — 비율이 높아질수록(금 강세·구리 약세) 경기둔화·위험회피 국면을, "
            "낮아질수록 경기확장·산업수요 개선 국면을 시사함"
        ),
        "wti": "원자재 수요 사이클을 공유하는 동행 지표이자, 채굴·제련·운송 비용을 통해 구리 생산원가에도 간접적으로 영향을 미침",
    },
    "상관관계 방향": {
        "dxy": "역상관 (구조적 음(-)의 상관)",
        "gold_copper_ratio": (
            f"임계값 방식(국면판단): 비율 ≤ {DEFAULT_GC_RATIO_BUY_THRESHOLD:g} 구리 우호적, "
            f"≥ {DEFAULT_GC_RATIO_SELL_THRESHOLD:g} 구리 비우호적 — 통계적 인과관계가 아닌 경험적 관행 지표"
        ),
        "wti": "정상관 (약함·저신뢰 — 문헌상 단기 효과만 확인됨)",
    },
}

ROW_ORDER = ["구조", "의미", "상관관계 방향"]

# Machine-readable version of the "상관관계 방향" row above, used by
# signals.py to decide both the main table's per-cell highlight AND each
# indicator's direction contribution to the copper_friendly score:
# - "inverse": indicator falling below its own MA is copper-friendly
#   (DXY: dollar weakness; gold_copper_ratio: ratio easing off its MA, i.e.
#   gold richening less / copper cheapening less, is the favorable-phase
#   direction — the opposite convention from Gold's gold/silver ratio).
# - "positive": indicator rising above its own MA is copper-friendly (WTI).
CORRELATION_DIRECTION = {
    "dxy": "inverse",
    "gold_copper_ratio": "inverse",
    "wti": "positive",
    "comex_copper_stock": "inverse",  # higher stock = more available supply = bearish
}

MONTHLY_STATIC_ROWS = {
    "구조": {
        "china_pmi": "중국 국가통계국(NBS) 또는 Caixin 제조업 구매관리자지수",
        "us_pmi": "미국 ISM 제조업 구매관리자지수",
    },
    "의미": {
        "china_pmi": "세계 최대 구리 소비국의 제조업 경기 확산/위축 여부를 매달 가장 먼저 보여주는 동행~약한 선행 지표",
        "us_pmi": "미국 제조업 수요 사이클을 보여주는 동행 지표로, 중국 PMI와 병행 참조되는 경우가 많음",
    },
    "상관관계 방향": {
        "china_pmi": "정상관 (50 기준선 — 50 이상 확장 국면은 구리 우호적, 이하는 비우호적)",
        "us_pmi": "정상관 (50 기준선 — 50 이상 확장 국면은 구리 우호적, 이하는 비우호적)",
    },
}
MONTHLY_ROW_ORDER = ["구조", "의미", "상관관계 방향"]
PMI_FAVORABLE_THRESHOLD = 50.0

FOOTNOTES = {
    "dxy": (
        "업계 자료 기준 상관계수 약 -0.65~-0.82 수준으로 보고되나 학술 피어리뷰로 확정된 수치는 "
        "아님. CME Group 자료에 따르면 2000~2019년엔 비교적 안정적이었던 이 관계가 팬데믹 이후 "
        "공급망 충격으로 깨진 사례가 있어, 특정 국면에서는 무력화될 수 있음."
    ),
    "gold_copper_ratio": (
        "'Dr. Copper' — 구리가 경기 선행지표로서의 별명을 가질 만큼 오랜 기간 시장에서 통용되어 온 "
        "경험적 관행 지표이며, 금과의 비율을 국면판단 보조지표로 활용하는 것 역시 시장 관행이지 "
        "통계적으로 검증된 인과관계는 아님."
    ),
    "wti": (
        "원자재발 인플레이션 기대 및 생산비용 채널을 통한 정(+)의 관계가 보고되나, 문헌상 '약함, "
        "낮은 신뢰도'로 명시된 지표로 다른 지표 대비 근거가 상대적으로 약함."
    ),
    "china_pmi": (
        "HSBC 리서치에 따르면 2022년 러시아-우크라이나 전쟁을 계기로 구리-PMI 상관관계가 일시적으로 "
        "0까지 떨어졌다가 최근 재차 강화되는 등, 국면에 따라 상관관계 자체가 크게 변동함. 히스토리컬 "
        "데이터는 비공식 3자 재배포 소스 기반이며 공식 통계와 오차가 있을 수 있음(백테스트 페이지 "
        "각주 참고)."
    ),
    "us_pmi": (
        "중국 PMI와 동일하게 국면에 따라 상관관계 강도가 크게 변동함(위 중국 PMI 각주 참고). "
        "히스토리컬 데이터는 비공식 3자 재배포 소스 기반이며 공식 통계와 오차가 있을 수 있음."
    ),
    "comex_copper_stock": (
        "무료 히스토리컬 아카이브가 없어 본 대시보드가 자체 축적을 시작한 실시간 지표로, 아직 "
        "충분한 기간이 쌓이기 전까지는 점수 계산·백테스트에서 제외됨(아래 COMEX 카드 참고)."
    ),
    "overall_limitation": (
        "다변량 모델 기준 표본외 설명력(R²)은 최대 약 18.5% 수준(2002~2014 LME 데이터 기준 계량연구)"
        "이라는 결과가 있어, 개별 지표 하나(또는 이들의 단순 가중합) 하나만으로 구리값을 예측하는 "
        "데는 뚜렷한 한계가 있음. 위 각 지표의 상관관계 방향은 이론적으로 근거가 있으나, 특정 국면"
        "(지정학적 충격, 공급망 붕괴 등)에서는 무력화될 수 있다는 취지로 이해할 것."
    ),
}
