"""Claim verification: plan check -> fact binding -> authorized formula -> verdict -> diagnosis.

A verification plan (written by the LLM) names the company, metric, period, basis and the text
spans that carry the number. The verifier re-reads every span itself: the LLM never converts a
number, and a plan whose fields disagree with its spans is abstained, not trusted.

Verdicts: supported | contradicted | abstain (VeriFin's Verified / Violated / Abstain).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from . import numbers as N
from .dart import DartClient, DartError
from .facts import (BASE_METRICS, DERIVED_METRICS, FactStore, Fact, Period, Unavailable, metric_id, metric_label,
                    normalize_period)
from .diagnose import diagnose

COMPARES = {"yoy": "전년 동기 대비", "qoq": "직전 분기 대비", "ye": "전년 말 대비"}
KIND_LABEL = {"value": "수치", "change": "비교", "direction": "방향"}


@dataclass
class Verdict:
    id: str
    status: str                    # supported | contradicted | abstain
    quote: str = ""
    kind: str = ""
    company: dict = field(default_factory=dict)
    metric: str = ""
    period: str = ""
    basis: str = ""
    basis_stated: bool = True
    claim: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    tolerance: dict = field(default_factory=dict)
    status_strict: str = ""
    diagnosis: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


# ---------------------------------------------------------------------- period span reading
_YEAR = re.compile(r"(\d{4})년")
_Q = re.compile(r"([1-4])\s*분기|([1-4])Q|Q([1-4])", re.IGNORECASE)


def read_period_span(span: str, as_of: str | None) -> dict:
    """What the sentence itself says about the period. Keys present only when the text implies them."""
    s = re.sub(r"\s+", "", span or "")
    out: dict = {}
    m = _YEAR.search(s)
    if m:
        out["year"] = int(m.group(1))
    elif as_of and re.search(r"지난해|작년|전년도", s) and not re.search(r"대비|동기", s):
        out["year"] = int(as_of[:4]) - 1
    elif as_of and re.search(r"올해|금년", s):
        out["year"] = int(as_of[:4])
    if re.search(r"1~3분기|1-3분기|3분기누적|3분기까지", s):
        out["part"], out["scope"] = "Q3", "cumulative"
    elif re.search(r"1~2분기|1-2분기|상반기|반기", s) and "하반기" not in s:
        out["part"], out["scope"] = "H1", "cumulative"
    elif "하반기" in s:
        out["part"] = "H2"
    else:
        q = _Q.search(s)
        if q:
            n = next(g for g in q.groups() if g)
            out["part"] = f"Q{n}"
            out["scope"] = "cumulative" if "누적" in s else "point" if re.search(r"분기말|분기기준", s) else "quarter"
        elif re.search(r"연간|한해|연결기준|사업연도|회계연도|작년|지난해|올해|금년|연말|기말|\d{4}년$", s) or _YEAR.search(s):
            out["part"] = "FY"
            if re.search(r"말|기말", s):
                out["scope"] = "point"
    return out


# ---------------------------------------------------------------------- verifier
class Verifier:
    def __init__(self, client: DartClient, mode: str = "default") -> None:
        if mode not in N.MODES:
            raise ValueError(f"mode must be one of {N.MODES}")
        self.client = client
        self.mode = mode

    # -------------------------------------------------------------- entry
    def verify(self, plan: dict, as_of: str | None = None) -> Verdict:
        v = Verdict(id=str(plan.get("id", "")), status="abstain", quote=str(plan.get("quote", "")), kind=str(plan.get("kind", "value")))
        try:
            self._check_plan(plan, v)
            corp = self._company(plan, v)
            store = FactStore(self.client, as_of)
            mid, period, basis = self._target(plan, v, as_of)
            if v.kind == "value":
                self._verify_value(store, corp, mid, period, basis, plan, v)
            else:
                self._verify_change(store, corp, mid, period, basis, plan, v)
        except Abstain as exc:
            v.status = "abstain"
            v.reasons.append(str(exc))
        except Unavailable as exc:
            v.status = "abstain"
            v.reasons.append(str(exc))
        except DartError as exc:
            v.status = "abstain"
            v.reasons.append(f"DART 오류: {exc}")
        return v

    # -------------------------------------------------------------- plan check
    def _check_plan(self, plan: dict, v: Verdict) -> None:
        quote = re.sub(r"\s+", "", v.quote)
        if not quote:
            raise Abstain("quote(원문 문장)가 비어 있습니다")
        for key in ("company", "metric", "period", "value", "change", "basis"):
            span = (plan.get(key) or {}).get("span") if isinstance(plan.get(key), dict) else None
            if span and re.sub(r"\s+", "", span) not in quote:
                raise Abstain(f"{key}.span '{span}'이 원문에 없습니다")
        if v.kind not in KIND_LABEL:
            raise Abstain(f"알 수 없는 kind {v.kind!r}")
        if v.kind == "value" and not (plan.get("value") or {}).get("span"):
            raise Abstain("value.span이 없습니다")
        if v.kind in ("change", "direction") and not (plan.get("change") or {}).get("span"):
            raise Abstain("change.span이 없습니다")
        hedge = plan.get("hedge_span")
        if hedge and re.sub(r"\s+", "", hedge) not in quote:
            raise Abstain(f"hedge_span '{hedge}'이 원문에 없습니다")
        if re.search(r"전망|예상|추정|컨센서스|목표|계획|가이던스|것으로보인다|할것", quote):
            v.notes.append("전망·추정 표현이 포함된 문장입니다. 공시 확정치와 비교합니다")

    def _company(self, plan: dict, v: Verdict) -> str:
        name = (plan.get("company") or {}).get("name") or (plan.get("company") or {}).get("span")
        if not name:
            raise Abstain("회사명이 없습니다")
        hits = self.client.resolve(name)
        if len(hits) != 1:
            raise Abstain(f"회사 '{name}'을 상장사 코드로 확정하지 못했습니다 (후보 {len(hits)}개)")
        corp = hits[0]
        v.company = {"name": corp["corp_name"], "stock_code": corp["stock_code"], "corp_code": corp["corp_code"]}
        info = self.client.company(corp["corp_code"])
        acc_mt = str(info.get("acc_mt") or "12")
        if acc_mt != "12":
            raise Abstain(f"{corp['corp_name']}의 결산월은 {acc_mt}월입니다. v1은 12월 결산 회사만 지원합니다")
        return corp["corp_code"]

    def _target(self, plan: dict, v: Verdict, as_of: str | None) -> tuple[str, Period, str]:
        m = plan.get("metric") or {}
        mid = metric_id(m.get("id") or m.get("span") or "")
        if mid is None:
            raise Abstain(f"지표 '{m.get('id') or m.get('span')}'는 v1 지원 범위 밖입니다")
        v.metric = mid
        p = plan.get("period") or {}
        if p.get("year") is None or not p.get("part"):
            raise Abstain("기간(year, part)이 없습니다. 기사 날짜로 연도를 특정할 수 없으면 보류합니다")
        try:
            period = normalize_period(int(p["year"]), str(p["part"]), p.get("scope"))
        except (ValueError, KeyError) as exc:
            raise Abstain(f"기간을 해석할 수 없습니다: {exc}")
        said = read_period_span(p.get("span", ""), as_of)
        if said.get("part") == "H2":
            raise Abstain("하반기 실적은 공시 보고서 열에 없어 v1에서 지원하지 않습니다")
        if "year" in said and said["year"] != period.year:
            raise Abstain(f"period.span은 {said['year']}년을 가리키지만 계획은 {period.year}년입니다")
        if "part" in said and said["part"] != period.part and not (said["part"] == "H1" and period.part == "Q2" and period.scope == "cumulative"):
            raise Abstain(f"period.span '{p.get('span')}'은 {said['part']}인데 계획은 {period.part}입니다")
        if said.get("scope") == "cumulative" and period.scope == "quarter":
            raise Abstain("period.span에 '누적'이 있지만 계획은 3개월 실적입니다")
        if not BASE_METRICS.get(mid, {}).get("flow", True) and period.scope != "point":
            period = Period(period.year, period.part, "point")
        if mid in DERIVED_METRICS and period.scope == "point":
            period = Period(period.year, period.part, "quarter" if period.part != "FY" else "annual")
        v.period = period.label()
        b = plan.get("basis") or {}
        basis = str(b.get("value") or "CFS").upper()
        if basis not in ("CFS", "OFS"):
            raise Abstain(f"basis는 CFS 또는 OFS여야 합니다 ({basis})")
        v.basis, v.basis_stated = basis, bool(b.get("span"))
        if not v.basis_stated:
            v.notes.append("연결/별도 기준이 문장에 없어 연결 기준으로 검증했습니다")
        return mid, period, basis

    # -------------------------------------------------------------- value claims
    def _expected(self, store: FactStore, corp: str, mid: str, period: Period, basis: str, prior: str | None = None) -> Fact:
        if mid in DERIVED_METRICS:
            return store.derived(corp, mid, period, basis, prior)
        if prior is None:
            return store.base(corp, mid, period, basis)
        if prior == "yoy":
            return store.prior_year(corp, mid, period, basis)
        if prior == "qoq":
            return store.prior_quarter(corp, mid, period, basis)
        return store.prior_year_end(corp, mid, period, basis)

    def _verify_value(self, store, corp, mid, period, basis, plan, v: Verdict) -> None:
        span = plan["value"]["span"]
        unit = DERIVED_METRICS[mid]["unit"] if mid in DERIVED_METRICS else "KRW"
        try:
            q = N.parse_rate(span) if unit == "%" else N.parse_amount(span)
        except N.ParseError as exc:
            raise Abstain(f"value.span을 읽을 수 없습니다: {exc}")
        if unit == "KRW" and q.value > 0 and (N.is_loss((plan.get("metric") or {}).get("span", "")) or N.is_loss(span)):
            q = N.Quantity(-q.value, q.unit, q.ulp, q.ulp_lenient, q.bound, q.upper, q.text)
            v.notes.append("손실/적자 표현이라 음수로 해석했습니다")
        fact = self._expected(store, corp, mid, period, basis)
        self._judge(q, fact.value, unit, v, fact)
        v.citations = fact.citations()
        if v.status == "contradicted":
            v.diagnosis = diagnose(store, corp, mid, period, basis, q, "value", None, self.mode)

    # -------------------------------------------------------------- change claims
    def _verify_change(self, store, corp, mid, period, basis, plan, v: Verdict) -> None:
        span = plan["change"]["span"]
        try:
            ch = N.parse_change(span)
        except N.ParseError as exc:
            raise Abstain(f"change.span을 읽을 수 없습니다: {exc}")
        compare = str((plan.get("change") or {}).get("compare") or "yoy").lower()
        if compare not in COMPARES:
            raise Abstain(f"compare는 yoy/qoq/ye 중 하나여야 합니다 ({compare})")
        if compare == "ye" and BASE_METRICS.get(mid, {}).get("flow", True):
            raise Abstain("전년 말 대비 비교는 재무상태표 항목에만 적용됩니다")
        cur = self._expected(store, corp, mid, period, basis)
        prev = self._expected(store, corp, mid, period, basis, compare)
        v.citations = cur.citations() + prev.citations()
        unit = cur.unit
        c, p = cur.value, prev.value
        fmt = (lambda x: N.fmt_krw(x)) if unit == "KRW" else (lambda x: N.fmt_rate(x, unit))
        v.expected = {"current": str(c), "previous": str(p), "unit": unit, "compare": COMPARES[compare],
                      "previous_period": prev.period.label(), "display": f"{fmt(c)} vs {prev.period.label()} {fmt(p)}"}
        kind = ch.kind
        # qualitative claims
        if kind in ("turn_profit", "turn_loss", "loss_narrow", "loss_widen", "stay_loss", "stay_profit", "direction", "flat"):
            truth = {
                "turn_profit": p < 0 < c, "turn_loss": c < 0 < p, "loss_narrow": p < 0 and c < 0 and c > p,
                "loss_widen": p < 0 and c < 0 and c < p, "stay_loss": p < 0 and c < 0, "stay_profit": p > 0 and c > 0,
                "direction": (c - p) * ch.direction > 0, "flat": p != 0 and abs((c - p) / p) * 100 < Decimal("1"),
            }[kind]
            v.claim = {"text": span, "kind": kind}
            v.status = "supported" if truth else "contradicted"
            v.status_strict = v.status
            v.tolerance = {"mode": self.mode, "rule": "정성 판단" + (" (보합: 변화 1% 미만)" if kind == "flat" else "")}
            if kind == "direction" and c == p:
                v.status = "contradicted"
            return
        q = ch.quantity
        if kind == "growth_pct" and unit == "%":
            # "영업이익률 20% 상승": %p difference or relative growth? Ambiguous -> abstain, show both readings.
            pp = c - p
            rel = (c - p) / abs(p) * 100 if p != 0 else None
            v.claim = {"value": str(q.value), "unit": "%", "text": q.text, "bound": q.bound, "display": N.fmt_rate(q.value)}
            v.diagnosis = [{"interpretation": "%p 차이로 읽으면", "value": str(pp), "display": N.fmt_rate(pp, "%p"),
                            "how": N.consistent(N.Quantity(q.value, "%p", q.ulp, q.ulp_lenient, q.bound, q.upper, q.text), pp, self.mode)[1]}]
            if rel is not None:
                v.diagnosis.append({"interpretation": "상대 증감률로 읽으면", "value": str(rel), "display": N.fmt_rate(rel),
                                    "how": N.consistent(q, rel, self.mode)[1]})
            raise Abstain("비율 지표의 변화를 '%'로 썼습니다. %p 차이인지 상대 증감률인지 모호해 판정하지 않습니다 (두 해석의 값은 diagnosis 참조)")
        if kind == "growth_pct":
            if p <= 0:
                raise Abstain(f"비교 기준 {prev.period.label()} 값이 {N.fmt_krw(p) if unit == 'KRW' else N.fmt_rate(p)}라 증감률을 정의할 수 없습니다 "
                              "(전환 여부로만 표현해야 합니다)")
            expected = (c - p) / p * 100
            self._judge(q, expected, "%", v, cur, prev)
        elif kind == "pp":
            if unit != "%":
                raise Abstain("%p 변화는 비율 지표(이익률·부채비율 등)에만 적용됩니다")
            self._judge(q, c - p, "%p", v, cur, prev)
        elif kind == "diff":
            if unit != "KRW":
                raise Abstain("금액 증감은 금액 지표에만 적용됩니다")
            self._judge(q, c - p, "KRW", v, cur, prev)
        else:
            raise Abstain(f"지원하지 않는 비교 유형 {kind}")
        if v.status == "contradicted":
            v.diagnosis = diagnose(store, corp, mid, period, basis, q, kind, compare, self.mode)

    # -------------------------------------------------------------- judgement
    def _judge(self, q: N.Quantity, expected: Decimal, unit: str, v: Verdict, fact: Fact, prev: Fact | None = None) -> None:
        ok, how = N.consistent(q, expected, self.mode)
        ok_s, how_s = N.consistent(q, expected, "strict")
        v.status = "supported" if ok else "contradicted"
        v.status_strict = "supported" if ok_s else "contradicted"
        fmt = (lambda x: N.fmt_krw(x)) if unit == "KRW" else (lambda x: N.fmt_rate(x, unit))
        v.claim = {"value": str(q.value), "unit": unit, "text": q.text, "bound": q.bound, "display": fmt(q.value)}
        v.expected = {**{k: x for k, x in v.expected.items() if k != "display"}, "value": str(expected), "unit": unit, "display": fmt(expected),
                      "derivation": fact.derivation or ("공시값" if prev is None else "")}
        if prev is not None:
            v.expected["formula"] = {"growth_pct": "(당기 − 전기) ÷ 전기 × 100", "%p": "당기 비율 − 전기 비율", "KRW": "당기 − 전기"}.get(
                "growth_pct" if unit == "%" and q.unit == "%" and v.kind == "change" else unit, "")
        v.tolerance = {"mode": self.mode, "unit": str(q.unit_for(self.mode)), "unit_strict": str(q.ulp), "how": how, "how_strict": how_s,
                       "rule": "표기 정밀도의 절반 (반올림)" + (" 또는 절사" if self.mode == "default" else "")}
        diff = expected - q.value
        v.expected["difference"] = str(diff)
        v.expected["difference_display"] = ("+" if diff > 0 else "") + fmt(diff)
        if ok and how == "trunc":
            v.notes.append("절사 표기로 일치합니다 (반올림으로는 불일치)")
        if ok and how == "approx":
            v.notes.append("완곡 표현('약', '수준' 등)이라 한 단위까지 허용했습니다")
        if ok and ok_s is False and how != "trunc":
            v.notes.append("엄격 모드(반올림만)에서는 불일치입니다")


class Abstain(Exception):
    pass
