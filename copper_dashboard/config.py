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
# in display order (main SMA table indicators first, then the copper-trend
# safety net, then monthly PMI). See "v2 시그널 로직" section below for
# copper_trend itself.
SCORE_INDICATOR_ORDER = ["copper_trend", "dxy", "gold_copper_ratio", "wti", "china_pmi", "us_pmi"]

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
# Weight rationale — v2 (see "v2 시그널 로직 (2026-09 수정)" section below for
# the full diagnosis this responds to; v1's original rationale is preserved
# in git history):
#   - copper_trend (1.2, new): copper's own price vs. its 200-day SMA. The
#     highest weight in the table on purpose — it's the safety net for the
#     exact failure mode v1 hit (every macro indicator staying "unfriendly"
#     through a structural copper bull run), so it must be able to outvote
#     the rest when it disagrees with them.
#   - DXY (0.9, down from 1.0): still the most literature-consistent link,
#     lightly trimmed to make room for copper_trend and reflect the
#     2024-2026 relationship weakening noted in FOOTNOTES.
#   - gold/copper ratio (0.8): unchanged weight — only its *judgment method*
#     changed (rolling percentile instead of an absolute threshold; see
#     below), the strength of the underlying phase-indicator idea did not.
#   - china_pmi / us_pmi (0.7 each): unchanged from v1.
#   - WTI (0.4 -> 0.3): trimmed further — the v1 diagnosis found WTI's
#     day-to-day shading contributing disproportionately to whipsaw for its
#     already-lowest-confidence link.
WEIGHTS = {
    "copper_trend": 1.2,
    "dxy": 0.9,
    "gold_copper_ratio": 0.8,
    "china_pmi": 0.7,
    "us_pmi": 0.7,
    "wti": 0.3,
}
MAX_RAW_SCORE = sum(WEIGHTS.values())  # 4.6 — every indicator at +1

# 0-100 score at/above this is treated as "copper-friendly" in the UI badge,
# and (see backtest.py StrategyParams) is also the v2 backtest's buy cutoff.
# v2 widens the v1 band (60/40 -> 65/35) as part of the whipsaw-suppression
# fix — see "v2 시그널 로직" below and README.md "v2 백테스트 검증 결과" for
# why: v1's tighter band let the score cross back and forth across a single
# cutoff on essentially noise, generating short-lived whipsaw trades. The
# widened, asymmetric band plus the persistence/min-holding rules below are
# meant to be evaluated together, not independently re-tuned by eye — see
# the "과최적화 금지" note in the v2 section.
SCORE_BUY_FRIENDLY_CUTOFF = 65.0
SCORE_SELL_UNFRIENDLY_CUTOFF = 35.0

# v1's original cutoffs, kept ONLY so the backtest validation script
# (scripts/validate_v2.py) can reproduce the exact v1 baseline for the
# required "v1 vs v2" comparison. Not read anywhere else.
LEGACY_V1_BUY_CUTOFF = 60.0
LEGACY_V1_SELL_CUTOFF = 40.0

# A PMI value not refreshed within this many days is excluded from the score
# (and flagged "데이터 오래됨" in the UI) rather than silently kept stale.
PMI_STALENESS_DAYS = 45

# ---------------------------------------------------------------------------
# gold/copper ratio ("Dr. Copper") phase judgment. Unlike Gold's
# gold/silver ratio (where a HIGH ratio is the bullish-gold signal), a HIGH
# gold/copper ratio means gold is expensive relative to copper — a classic
# recession/risk-off signature (weak industrial demand) — so a LOW ratio is
# the copper-friendly side here.
#
# v1 used a pair of FIXED absolute thresholds (440/620, the 25th/75th
# percentile of 2016-2026 history). That broke down once gold entered a
# structural bull run in 2024-2026: the ratio's numerator re-based
# permanently higher, so the ratio sat above 620 ("copper-unfriendly")
# almost continuously through 2023-2026 even while copper itself rallied
# hard — an absolute threshold on a non-stationary series just tracks
# whichever regime it was calibrated in. v2 replaces this with a ROLLING
# PERCENTILE RANK (see GC_RATIO_USE_ROLLING_PERCENTILE below): "is today's
# ratio low/high relative to its own last N days", which re-centers
# automatically as gold (or copper) re-bases. See "v2 시그널 로직" below.
GC_RATIO_USE_ROLLING_PERCENTILE = True  # False reproduces the v1 absolute-threshold behavior
DEFAULT_GC_RATIO_BUY_THRESHOLD = 440.0   # legacy (v1) absolute threshold — ratio <= this => friendly
DEFAULT_GC_RATIO_SELL_THRESHOLD = 620.0  # legacy (v1) absolute threshold — ratio >= this => unfriendly

# v2 rolling-percentile parameters, all tunable here per the task's "모든
# 신규 상수는 config.py에 모아서" requirement. 504 trading days ~= 2 calendar
# years; 30/70 is the same "opposite quartiles" shape as v1's original
# 25th/75th percentile calibration, just recomputed on a trailing window
# instead of the whole fixed 2016-2026 sample.
GC_RATIO_ROLLING_WINDOW = 504
GC_RATIO_PERCENTILE_BUY = 30.0   # rolling percentile rank <= this => copper-friendly
GC_RATIO_PERCENTILE_SELL = 70.0  # rolling percentile rank >= this => copper-unfriendly

# ---------------------------------------------------------------------------
# copper's own price vs. its 200-day SMA — new in v2, see "v2 시그널 로직"
# below. This is deliberately NOT folded into INDICATOR_ORDER/MA_WINDOWS
# above: it's a single-window trend filter on copper itself (not a 60/30/5
# breakout table entry), so it gets its own card (build_table.build_copper_trend_card)
# the same way PMI/COMEX get their own cards instead of being forced into
# the daily SMA table's shape.
COPPER_TREND_ENABLED = True
COPPER_TREND_SMA_WINDOW = 200

# While copper's close is above its own 200-day SMA, a sell signal never
# fully liquidates — it only trims to this fraction of the position. Turn
# off to always fully exit on a sell signal regardless of the 200-day trend
# (the pre-v2 behavior).
COPPER_TREND_PARTIAL_EXIT_ENABLED = True
COPPER_TREND_PARTIAL_EXIT_FRACTION = 0.5

# ---------------------------------------------------------------------------
# whipsaw suppression: v1's single 60/40 cutoff crossed back and forth on
# essentially day-to-day noise (see README.md "v2 백테스트 검증 결과" for the
# diagnosis). v2 adds an asymmetric buy(65)/sell(35) band (see
# SCORE_BUY_FRIENDLY_CUTOFF/SCORE_SELL_UNFRIENDLY_CUTOFF above — the 35-65
# gap is "hold whatever position you already have"), a persistence
# requirement before a crossing is acted on, and a minimum holding period.
WHIPSAW_SUPPRESSION_ENABLED = True
SIGNAL_CONFIRMATION_DAYS = 3  # a cutoff must be held for this many consecutive
# trading days before a trade actually fires (ignores 1-2 day spikes)
MIN_HOLDING_DAYS = 10  # trading days after entry during which a sell is not even evaluated
# (the stop loss below is the explicit, documented exception to this)

# ---------------------------------------------------------------------------
# DXY judgment method — v2 candidate change, see "v2 시그널 로직" below.
# "sma": v1's method — copper-friendly when DXY sits below ALL of its
#   5/30/60-day SMAs (same all-windows-agree rule as WTI).
# "roc": v2 candidate — DXY's own N-day rate of change; negative (dollar
#   weakening) => copper-friendly, positive => unfriendly. The task brief
#   asks for both to be backtested and the better one kept — see
#   scripts/validate_v2.py and README.md for that comparison; this constant
#   selects which one the live dashboard/backtest actually uses.
DXY_SIGNAL_METHOD = "roc"  # "roc" or "sma"
DXY_ROC_WINDOW = 60

# ---------------------------------------------------------------------------
# stop loss — v1 had none, and its -45.9% MDD was judged unacceptable.
# Unconditional exit once price falls this fraction below entry, bypassing
# both the min-holding period and the signal-confirmation requirement above
# (a stop loss is not a "signal", it's a hard risk limit). Re-entry after a
# stop-out still requires the normal buy conditions (cutoff + persistence)
# to be met again from scratch.
STOP_LOSS_ENABLED = True
STOP_LOSS_PCT = -0.15

INDICATOR_META = {
    "dxy": {
        "label": "달러인덱스",
        "source": "Yahoo Finance (DX-Y.NYB)",
        "unit": "",
        "decimals": 2,
    },
    "copper_trend": {
        "label": "구리 자체 추세 (200일선)",
        "source": "yfinance HG=F",
        "unit": "$",
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
    "copper_trend": "positive",  # close above its own 200-day SMA is copper-friendly
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
        "공급망 충격으로 깨진 사례가 있어, 특정 국면에서는 무력화될 수 있음. v1은 자체 이동평균 "
        "대비 위치로 판정했으나 이 방식이 whipsaw(하루 단위 신호 반전)의 주요 원인 중 하나로 "
        "지목되어, v2는 60일 변화율(rate of change) 기준으로 전환함(config.DXY_SIGNAL_METHOD로 "
        "전환/복귀 가능 — scripts/validate_v2.py의 두 방식 비교 결과 참고)."
    ),
    "gold_copper_ratio": (
        "'Dr. Copper' — 구리가 경기 선행지표로서의 별명을 가질 만큼 오랜 기간 시장에서 통용되어 온 "
        "경험적 관행 지표이며, 금과의 비율을 국면판단 보조지표로 활용하는 것 역시 시장 관행이지 "
        "통계적으로 검증된 인과관계는 아님. v1은 이 비율에 고정 절대 임계값(440/620)을 썼으나, "
        "2024~2026년 금의 구조적 대세 상승으로 비율이 영구적으로 레벨업하며 무력화되어(백테스트에서 "
        "매수신호가 사실상 소멸) v2에서 롤링 백분위 방식으로 교체함 — 절대 임계값은 이런 비정상"
        "(non-stationary) 시계열에 적합하지 않음."
    ),
    "copper_trend": (
        "v2에서 신규 추가된 안전장치 지표. 위 각주들이 공통으로 지적하는 한계 — 거시 상관관계가 "
        "특정 국면(지정학적 충격, 공급망 붕괴, 관세 이슈 등)에서 무력화될 수 있다는 점 — 을 "
        "보완하기 위해, 거시 환경이 아닌 구리 가격 자체의 200일 이동평균 추세를 별도 지표로 "
        "포함함. 다른 지표 전부가 '비우호적'을 가리켜도 구리가 실제로 강한 추세를 타고 있다면 "
        "그 사실 자체가 신호에 반영되도록 하는 목적이며, 통계적으로 검증된 선행지표는 아니고 "
        "가격 추세 추종(trend-following)이라는 별도의 가정에 기반함."
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
