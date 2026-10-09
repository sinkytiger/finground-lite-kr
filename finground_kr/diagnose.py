"""Trap diagnosis: which *other* reading of the filing would make a contradicted claim come out right?

VeriFin returns an unsatisfiable core naming the conflicting facts; for Korean filings the useful
equivalent is a list of common misreadings (separate vs consolidated, cumulative vs quarterly, prior
year, non-controlling interests, unit slips, wrong growth formula). Each alternative is recomputed
from filed values and reported only when it matches the claim within the same tolerance. A single
match is a strong hint; several matches are listed without choosing.
"""

from __future__ import annotations

from decimal import Decimal

from . import numbers as N
from .facts import BASE_METRICS, DERIVED_METRICS, FactStore, Period, Unavailable, metric_label

OTHER_BASIS = {"CFS": "OFS", "OFS": "CFS"}
BASIS_KO = {"CFS": "연결", "OFS": "별도"}


def _value(store: FactStore, corp: str, mid: str, period: Period, basis: str, prior: str | None = None) -> Decimal:
    if mid in DERIVED_METRICS:
        return store.derived(corp, mid, period, basis, prior).value
    if prior is None:
        return store.base(corp, mid, period, basis).value
    if prior == "yoy":
        return store.prior_year(corp, mid, period, basis).value
    if prior == "qoq":
        return store.prior_quarter(corp, mid, period, basis).value
    return store.prior_year_end(corp, mid, period, basis).value


def expected_for(store: FactStore, corp: str, mid: str, period: Period, basis: str, kind: str, compare: str | None,
                 prior: str | None = None) -> Decimal:
    """The number a claim of `kind` asserts, under one reading of the filing."""
    if kind == "value":
        return _value(store, corp, mid, period, basis, prior)
    c = _value(store, corp, mid, period, basis)
    p = _value(store, corp, mid, period, basis, compare or "yoy")
    if kind == "growth_pct":
        if p <= 0:
            raise Unavailable("비교 기준이 0 이하")
        return (c - p) / p * 100
    return c - p  # pp, diff


def diagnose(store: FactStore, corp: str, mid: str, period: Period, basis: str, q: N.Quantity, kind: str,
             compare: str | None, mode: str) -> list[dict]:
    alts: list[tuple[str, dict]] = []   # (interpretation label, kwargs for expected_for)
    base = dict(mid=mid, period=period, basis=basis, kind=kind, compare=compare)
    flow = BASE_METRICS.get(mid, {}).get("flow", True)

    alts.append((f"{BASIS_KO[OTHER_BASIS[basis]]} 기준 값", {**base, "basis": OTHER_BASIS[basis]}))
    if flow and period.part in ("Q2", "Q3"):
        if period.scope == "quarter":
            alts.append((f"{period.year}년 {'상반기' if period.part == 'Q2' else '3분기'} 누적 값", {**base, "period": Period(period.year, period.part, "cumulative")}))
    if flow and period.scope == "cumulative" and period.part in ("H1", "Q3"):
        qpart = "Q2" if period.part == "H1" else "Q3"
        alts.append((f"{period.year}년 {qpart[-1]}분기 3개월 값", {**base, "period": Period(period.year, qpart, "quarter")}))
    if kind == "value":
        alts.append(("전년 동기 값", {**base, "prior": "yoy"}))
        if period.prior_quarter() is not None:
            alts.append(("직전 분기 값", {**base, "prior": "qoq"}))
        if flow and period.part != "FY" and period.scope != "point":
            alts.append((f"{period.year}년 연간 값", {**base, "period": Period(period.year, "FY", "annual")}))
            alts.append((f"{period.year - 1}년 연간 값", {**base, "period": Period(period.year - 1, "FY", "annual")}))
    else:
        if compare != "qoq" and period.scope in ("quarter", "point"):
            alts.append(("직전 분기 대비로 계산한 값", {**base, "compare": "qoq"}))
        if compare != "yoy":
            alts.append(("전년 동기 대비로 계산한 값", {**base, "compare": "yoy"}))
    if mid == "net_income_owners":
        alts.append(("비지배지분 포함 당기순이익", {**base, "mid": "net_income"}))
    elif mid == "net_income":
        alts.append(("지배기업 소유주지분 순이익", {**base, "mid": "net_income_owners"}))
    if mid == "equity_total":
        alts.append(("지배기업 소유주지분 자본", {**base, "mid": "equity_owners"}))
    elif mid == "equity_owners":
        alts.append(("자본총계(비지배지분 포함)", {**base, "mid": "equity_total"}))

    mode = "default"  # diagnosis is a hint, not an acceptance decision: always use the lenient tolerance

    def near(a: Decimal, b: Decimal) -> bool:  # gross-error hints (unit, sign) only need to be in the neighbourhood
        return b != 0 and abs(a - b) <= abs(b) * Decimal("0.02")
    out: list[dict] = []
    checked = 0
    for label, kw in alts:
        try:
            alt = expected_for(store, corp, **kw)
        except Unavailable:
            continue
        checked += 1
        ok, how = N.consistent(q, alt, mode)
        if ok:
            out.append({"interpretation": label, "value": str(alt), "display": _fmt(alt, q.unit), "how": how})

    # arithmetic slips that need no new facts
    try:
        exp = expected_for(store, corp, **base)
    except Unavailable:
        exp = None
    if exp is not None:
        checked += 1
        if kind == "growth_pct" and compare:
            try:
                c = _value(store, corp, mid, period, basis)
                p = _value(store, corp, mid, period, basis, compare)
                if c != 0:
                    wrong = (c - p) / abs(c) * 100
                    if N.consistent(q, wrong, mode)[0]:
                        out.append({"interpretation": "분모를 당기 값으로 둔 증감률", "value": str(wrong), "display": _fmt(wrong, "%"), "how": "formula"})
                if N.consistent(q, -exp, mode)[0]:
                    out.append({"interpretation": "부호가 반대인 증감률", "value": str(-exp), "display": _fmt(-exp, "%"), "how": "sign"})
            except Unavailable:
                pass
        if kind == "pp" and compare and mid in DERIVED_METRICS:
            try:
                c = _value(store, corp, mid, period, basis)
                p = _value(store, corp, mid, period, basis, compare)
                if p != 0:
                    pct = (c - p) / abs(p) * 100
                    if N.consistent(q, pct, mode)[0]:
                        out.append({"interpretation": "%p가 아니라 비율의 증감률(%)", "value": str(pct), "display": _fmt(pct, "%"), "how": "unit"})
            except Unavailable:
                pass
        if kind == "value" and q.value != 0 and (near(-q.value, exp) or N.consistent(N.Quantity(-q.value, q.unit, q.ulp, q.ulp_lenient, q.bound, None, q.text), exp, mode)[0]):
            out.append({"interpretation": "손익 부호가 반대 (이익과 손실 착오)", "value": str(exp), "display": _fmt(exp, q.unit), "how": "sign"})
        if kind in ("value", "diff") and q.unit == "KRW":
            for k in (1, 2, 3, 4, -1, -2, -3, -4):
                scaled = q.value * (Decimal(10) ** k)
                sq = N.Quantity(scaled, q.unit, q.ulp * Decimal(10) ** k, q.ulp_lenient * Decimal(10) ** k, q.bound,
                                None if q.upper is None else q.upper * Decimal(10) ** k, q.text)
                if near(scaled, exp) or N.consistent(sq, exp, mode)[0]:
                    out.append({"interpretation": f"단위 착오 (주장값 × 10^{k}이면 일치)", "value": str(exp), "display": _fmt(exp, "KRW"), "how": "unit"})
                    break
        if kind == "value" and q.unit == "%":
            for k in (2, -2):
                scaled = q.value * (Decimal(10) ** k)
                sq = N.Quantity(scaled, q.unit, q.ulp * Decimal(10) ** k, q.ulp_lenient * Decimal(10) ** k, q.bound, None, q.text)
                if N.consistent(sq, exp, mode)[0]:
                    out.append({"interpretation": "비율 단위 착오 (소수/퍼센트 혼동)", "value": str(exp), "display": _fmt(exp, "%"), "how": "unit"})
                    break
    for o in out:
        o["checked_alternatives"] = checked
    return out


def _fmt(value: Decimal, unit: str) -> str:
    return N.fmt_krw(value) if unit == "KRW" else N.fmt_rate(value, unit)
