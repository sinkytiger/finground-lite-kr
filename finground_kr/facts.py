"""Facts: metric registry, period arithmetic and value binding against DART reports.

A Fact is a filed or derived number with full provenance. Values are bound from reports only
(never from the claim), following VeriFin's "value-blind" binding. Derived facts (Q4 = FY - Q3
cumulative, TTM, ratios) record every component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .dart import PERIOD_LABEL, BASIS_LABEL, DartClient, DartError, Report, Row

# ---------------------------------------------------------------------- metric registry
# id -> (statement kinds, XBRL account ids, normalized Korean names, label)
BASE_METRICS: dict[str, dict] = {
    "revenue": {"sj": ("IS", "CIS"), "ids": ("ifrs-full_Revenue", "ifrs_Revenue"),
                "names": ("매출액", "매출", "수익", "영업수익", "매출및지분법손익"), "label": "매출액", "flow": True},
    "cost_of_sales": {"sj": ("IS", "CIS"), "ids": ("ifrs-full_CostOfSales", "ifrs_CostOfSales"), "names": ("매출원가",), "label": "매출원가", "flow": True},
    "gross_profit": {"sj": ("IS", "CIS"), "ids": ("ifrs-full_GrossProfit", "ifrs_GrossProfit"), "names": ("매출총이익",), "label": "매출총이익", "flow": True},
    "operating_income": {"sj": ("IS", "CIS"), "ids": ("dart_OperatingIncomeLoss",), "names": ("영업이익", "영업손실"), "label": "영업이익", "flow": True},
    "net_income": {"sj": ("IS", "CIS"), "ids": ("ifrs-full_ProfitLoss", "ifrs_ProfitLoss"),
                   "names": ("당기순이익", "당기순손실", "분기순이익", "반기순이익", "분기순손실", "반기순손실", "당기순이익(손실)"),
                   "label": "당기순이익", "flow": True},
    "net_income_owners": {"sj": ("IS", "CIS"), "ids": ("ifrs-full_ProfitLossAttributableToOwnersOfParent", "ifrs_ProfitLossAttributableToOwnersOfParent"),
                          "names": ("지배기업소유주지분", "지배기업의소유주에게귀속되는당기순이익", "지배회사지분반기순이익", "지배회사지분분기순이익",
                                    "지배기업소유주지분순이익", "지배주주순이익", "지배기업의소유주지분"),
                          "label": "지배기업 소유주지분 순이익", "flow": True},
    "assets": {"sj": ("BS",), "ids": ("ifrs-full_Assets", "ifrs_Assets"), "names": ("자산총계",), "label": "자산총계", "flow": False},
    "liabilities": {"sj": ("BS",), "ids": ("ifrs-full_Liabilities", "ifrs_Liabilities"), "names": ("부채총계",), "label": "부채총계", "flow": False},
    "equity_total": {"sj": ("BS",), "ids": ("ifrs-full_Equity", "ifrs_Equity"), "names": ("자본총계",), "label": "자본총계", "flow": False},
    "equity_owners": {"sj": ("BS",), "ids": ("ifrs-full_EquityAttributableToOwnersOfParent", "ifrs_EquityAttributableToOwnersOfParent"),
                      "names": ("지배기업소유주지분", "지배기업의소유주에게귀속되는자본", "지배기업소유주귀속자본"), "label": "지배기업 소유주지분 자본", "flow": False},
}

# derived metrics: (formula over base metric ids, unit, label, definition source)
DERIVED_METRICS: dict[str, dict] = {
    "operating_margin": {"formula": "operating_income / revenue * 100", "unit": "%", "label": "영업이익률",
                         "source": "영업이익률 = 영업이익 ÷ 매출액 × 100 (일반 정의)"},
    "net_margin": {"formula": "net_income / revenue * 100", "unit": "%", "label": "순이익률",
                   "source": "순이익률 = 당기순이익 ÷ 매출액 × 100 (일반 정의)"},
    "gross_margin": {"formula": "gross_profit / revenue * 100", "unit": "%", "label": "매출총이익률",
                     "source": "매출총이익률 = 매출총이익 ÷ 매출액 × 100 (일반 정의)"},
    "debt_ratio": {"formula": "liabilities / equity_total * 100", "unit": "%", "label": "부채비율",
                   "source": "부채비율 = 부채총계 ÷ 자본총계 × 100 (일반 정의)"},
    "equity_ratio": {"formula": "equity_total / assets * 100", "unit": "%", "label": "자기자본비율",
                     "source": "자기자본비율 = 자본총계 ÷ 자산총계 × 100 (일반 정의)"},
}

ALIASES = {  # Korean metric words the planner may emit -> metric id
    "매출": "revenue", "매출액": "revenue", "매출고": "revenue", "영업수익": "revenue",
    "영업이익": "operating_income", "영업손실": "operating_income", "영업손익": "operating_income",
    "순이익": "net_income", "당기순이익": "net_income", "순손실": "net_income", "당기순손실": "net_income", "분기순이익": "net_income", "반기순이익": "net_income",
    "지배주주순이익": "net_income_owners", "지배기업소유주지분순이익": "net_income_owners", "지배주주귀속순이익": "net_income_owners",
    "자산총계": "assets", "총자산": "assets", "부채총계": "liabilities", "총부채": "liabilities",
    "자본총계": "equity_total", "총자본": "equity_total", "자기자본": "equity_total",
    "매출원가": "cost_of_sales", "매출총이익": "gross_profit",
    "영업이익률": "operating_margin", "순이익률": "net_margin", "매출총이익률": "gross_margin", "부채비율": "debt_ratio", "자기자본비율": "equity_ratio",
}


def metric_id(name: str) -> str | None:
    key = str(name).replace(" ", "")
    if key in BASE_METRICS or key in DERIVED_METRICS:
        return key
    return ALIASES.get(key)


def metric_label(mid: str) -> str:
    return (BASE_METRICS.get(mid) or DERIVED_METRICS.get(mid) or {}).get("label", mid)


# ---------------------------------------------------------------------- periods
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
SCOPES = ("quarter", "cumulative", "annual", "point")


@dataclass(frozen=True)
class Period:
    year: int
    part: str      # Q1 | Q2 | Q3 | Q4 | H1 | FY
    scope: str     # quarter (3 months) | cumulative (from Jan) | annual | point (balance-sheet date)

    def label(self) -> str:
        names = {"Q1": "1분기", "Q2": "2분기", "Q3": "3분기", "Q4": "4분기", "H1": "상반기", "FY": "연간"}
        base = f"{self.year}년 {names[self.part]}"
        if self.scope == "cumulative" and self.part in ("Q2", "Q3", "H1"):
            return base + " 누적" if self.part != "H1" else base
        if self.scope == "point":
            return f"{self.year}년 {names[self.part]} 말" if self.part != "FY" else f"{self.year}년 말"
        return base

    def prior_year(self) -> "Period":
        return Period(self.year - 1, self.part, self.scope)

    def prior_quarter(self) -> "Period | None":
        if self.scope not in ("quarter", "point") or self.part not in QUARTERS + ("FY",):
            return None
        order = ["Q1", "Q2", "Q3", "Q4"]
        part = "Q4" if self.part == "FY" else self.part
        i = order.index(part)
        return Period(self.year - 1, "Q4", self.scope) if i == 0 else Period(self.year, order[i - 1], self.scope)


def normalize_period(year: int, part: str, scope: str | None) -> Period:
    part = part.upper()
    if part == "H1" and scope in (None, "cumulative", "quarter"):
        scope = "cumulative"
    if part == "FY":
        scope = scope if scope == "point" else "annual"
    if part in ("Q2", "Q3") and scope is None:
        scope = "quarter"
    if part == "Q1" and scope in (None, "cumulative"):
        scope = "quarter"
    if part == "Q4":
        scope = scope if scope == "point" else "quarter"
    if scope not in SCOPES:
        raise ValueError(f"bad scope {scope!r}")
    return Period(int(year), part, scope)


# ---------------------------------------------------------------------- facts
@dataclass
class Source:
    report: Report
    row: Row
    column: str                  # thstrm | thstrm_add | frmtrm | frmtrm_q | frmtrm_add | bfefrmtrm
    value: Decimal

    def describe(self) -> str:
        col = {"thstrm": "당기", "thstrm_add": "당기 누적", "frmtrm": "전기", "frmtrm_q": "전년 동기(3개월)",
               "frmtrm_add": "전년 동기 누적", "bfefrmtrm": "전전기"}[self.column]
        return f"{self.report.label} {self.row.sj_div} '{self.row.label}' {col} 열 = {self.value:,}"


@dataclass
class Fact:
    metric: str
    period: Period
    basis: str
    value: Decimal
    unit: str = "KRW"
    sources: list[Source] = field(default_factory=list)
    derivation: str = ""         # e.g. "FY − 3분기 누적", "TTM", "operating_income / revenue * 100"
    components: dict = field(default_factory=dict)

    @property
    def filed(self) -> str | None:
        dates = [s.report.filed for s in self.sources if s.report.filed]
        return max(dates) if dates else None

    def citations(self) -> list[dict]:
        return [{"report": s.report.label, "rcept_no": s.report.rcept_no, "filed": s.report.filed, "statement": s.row.sj_div,
                 "account": s.row.label, "column": s.column, "value": str(s.value), "url": s.report.url}
                for s in self.sources]


class Unavailable(Exception):
    """A fact that cannot be bound: missing report (as of the date), missing account, unsupported period."""


class FactStore:
    def __init__(self, client: DartClient, as_of: str | None = None) -> None:
        self.client = client
        self.as_of = as_of   # YYYY-MM-DD; reports filed after this date are invisible

    # -------------------------------------------------------------- reports
    def report(self, corp_code: str, year: int, period: str, basis: str) -> Report:
        try:
            rep = self.client.report(corp_code, year, period, basis)
        except DartError as exc:
            raise Unavailable(f"DART 조회 실패: {exc}") from exc
        label = f"{year}년 {PERIOD_LABEL[period]}({BASIS_LABEL[basis]})"
        if rep is None:
            raise Unavailable(f"{label}가 DART에 없습니다")
        if self.as_of and rep.filed and rep.filed > self.as_of:
            raise Unavailable(f"{label}는 {rep.filed}에 제출되어 기준일 {self.as_of} 이후입니다")
        return rep

    def _row(self, rep: Report, metric: str) -> Row:
        spec = BASE_METRICS[metric]
        row = rep.find(spec["sj"], spec["ids"], spec["names"])
        if row is None:
            raise Unavailable(f"{rep.label}에서 '{spec['label']}' 계정을 찾지 못했습니다")
        return row

    def _bind(self, corp: str, metric: str, year: int, rp: str, basis: str, column: str, period: Period, derivation: str = "") -> Fact:
        rep = self.report(corp, year, rp, basis)
        row = self._row(rep, metric)
        value = row.column(column)
        if value is None:
            raise Unavailable(f"{rep.label} '{row.account_nm}'의 {column} 열이 비어 있습니다")
        return Fact(metric, period, basis, value, "KRW", [Source(rep, row, column, value)], derivation)

    # -------------------------------------------------------------- base metrics
    def base(self, corp: str, metric: str, period: Period, basis: str) -> Fact:
        """Value of a base metric for a period; prior-year values come from the same report's comparative
        columns when possible (they reflect the issuer's own restated comparatives)."""
        if metric not in BASE_METRICS:
            raise Unavailable(f"알 수 없는 지표 {metric}")
        flow = BASE_METRICS[metric]["flow"]
        y, p, s = period.year, period.part, period.scope
        if not flow:  # balance sheet: point in time
            rp = {"Q1": "Q1", "Q2": "H1", "H1": "H1", "Q3": "Q3", "Q4": "FY", "FY": "FY"}[p]
            return self._bind(corp, metric, y, rp, basis, "thstrm", Period(y, p, "point"))
        if s == "annual" or p == "FY":
            return self._bind(corp, metric, y, "FY", basis, "thstrm", Period(y, "FY", "annual"))
        if s == "cumulative":
            rp = {"Q1": "Q1", "Q2": "H1", "H1": "H1", "Q3": "Q3"}.get(p)
            if rp is None:
                raise Unavailable(f"{p} 누적은 지원하지 않습니다")
            return self._bind(corp, metric, y, rp, basis, "thstrm_add", period)
        # quarter (3 months)
        if p in ("Q1", "Q2", "Q3"):
            rp = {"Q1": "Q1", "Q2": "H1", "Q3": "Q3"}[p]
            return self._bind(corp, metric, y, rp, basis, "thstrm", period)
        if p == "Q4":
            fy = self._bind(corp, metric, y, "FY", basis, "thstrm", Period(y, "FY", "annual"))
            q3 = self._bind(corp, metric, y, "Q3", basis, "thstrm_add", Period(y, "Q3", "cumulative"))
            return Fact(metric, period, basis, fy.value - q3.value, "KRW", fy.sources + q3.sources,
                        "4분기 = 연간 − 3분기 누적", {"FY": fy.value, "Q3_cum": q3.value})
        raise Unavailable(f"지원하지 않는 기간 {period}")

    def prior_year(self, corp: str, metric: str, period: Period, basis: str) -> Fact:
        """Same period one year earlier, preferring the comparative column of the current report."""
        flow = BASE_METRICS[metric]["flow"]
        y, p, s = period.year, period.part, period.scope
        prior = period.prior_year()
        if flow and p in ("Q1", "Q2", "Q3", "H1") and s in ("quarter", "cumulative"):
            rp = {"Q1": "Q1", "Q2": "H1", "H1": "H1", "Q3": "Q3"}[p]
            col = "frmtrm_q" if s == "quarter" else "frmtrm_add"
            try:
                return self._bind(corp, metric, y, rp, basis, col, prior, "전년 동기 (비교 열)")
            except Unavailable:
                return self.base(corp, metric, prior, basis)
        if flow and (s == "annual" or p == "FY"):
            try:
                return self._bind(corp, metric, y, "FY", basis, "frmtrm", prior, "전기 (비교 열)")
            except Unavailable:
                return self.base(corp, metric, prior, basis)
        if flow and p == "Q4":
            fy = self._bind(corp, metric, y, "FY", basis, "frmtrm", Period(y - 1, "FY", "annual"))
            q3 = self._bind(corp, metric, y, "Q3", basis, "frmtrm_add", Period(y - 1, "Q3", "cumulative"))
            return Fact(metric, prior, basis, fy.value - q3.value, "KRW", fy.sources + q3.sources,
                        "전년 4분기 = 전년 연간 − 전년 3분기 누적", {"FY": fy.value, "Q3_cum": q3.value})
        if not flow and p == "FY":
            return self._bind(corp, metric, y, "FY", basis, "frmtrm", Period(y - 1, "FY", "point"), "전기말 (비교 열)")
        return self.base(corp, metric, prior, basis)  # balance sheet, same quarter last year: separate report

    def prior_year_end(self, corp: str, metric: str, period: Period, basis: str) -> Fact:
        """Balance-sheet comparison base used by news: 전년 말 대비."""
        rp = {"Q1": "Q1", "Q2": "H1", "H1": "H1", "Q3": "Q3", "Q4": "FY", "FY": "FY"}[period.part]
        return self._bind(corp, metric, period.year, rp, basis, "frmtrm", Period(period.year - 1, "FY", "point"), "전기말 (비교 열)")

    def prior_quarter(self, corp: str, metric: str, period: Period, basis: str) -> Fact:
        pq = period.prior_quarter()
        if pq is None:
            raise Unavailable("직전 분기 비교는 3개월 실적에만 적용됩니다")
        return self.base(corp, metric, pq, basis)

    def ttm(self, corp: str, metric: str, period: Period, basis: str) -> Fact:
        """Trailing twelve months ending at a quarter: FY(prev) − cum(prev, same) + cum(cur)."""
        y, p = period.year, period.part
        if p in ("FY", "Q4") or period.scope == "annual":
            return self.base(corp, metric, Period(y, "FY", "annual"), basis)
        cum_part = "H1" if p in ("Q2", "H1") else p
        cur = self.base(corp, metric, Period(y, cum_part, "cumulative"), basis)
        prev = self.prior_year(corp, metric, Period(y, cum_part, "cumulative"), basis)
        fy = self.base(corp, metric, Period(y - 1, "FY", "annual"), basis)
        return Fact(metric, period, basis, fy.value - prev.value + cur.value, "KRW", fy.sources + prev.sources + cur.sources,
                    "TTM = 전년 연간 − 전년 동기 누적 + 당기 누적", {"FY_prev": fy.value, "cum_prev": prev.value, "cum_cur": cur.value})

    # -------------------------------------------------------------- derived metrics
    def derived(self, corp: str, metric: str, period: Period, basis: str, prior: str | None = None) -> Fact:
        """Ratio metrics. `prior` = None | 'yoy' | 'qoq' | 'ye' selects the comparison base for each component."""
        spec = DERIVED_METRICS[metric]
        num, _, rest = spec["formula"].partition(" / ")
        den = rest.split(" ")[0]
        comps = {}
        sources = []
        for mid in (num, den):
            if prior is None:
                f = self.base(corp, mid, period, basis)
            elif prior == "yoy":
                f = self.prior_year(corp, mid, period, basis)
            elif prior == "qoq":
                f = self.prior_quarter(corp, mid, period, basis)
            else:
                f = self.prior_year_end(corp, mid, period, basis)
            comps[mid] = f.value
            sources += f.sources
        if comps[den] == 0:
            raise Unavailable(f"{metric_label(den)}이 0이라 {spec['label']}을 계산할 수 없습니다")
        value = comps[num] / comps[den] * 100
        per = period if prior is None else (period.prior_year() if prior == "yoy" else period.prior_quarter() if prior == "qoq" else Period(period.year - 1, "FY", "point"))
        return Fact(metric, per, basis, value, spec["unit"], sources, spec["formula"], comps)
