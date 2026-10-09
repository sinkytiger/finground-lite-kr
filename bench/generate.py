"""Benchmark generator: claims written *from* filed values, with injected trap errors.

Following VeriFin's XBRLFiling construction in reverse: start from a value the company filed, render a
Korean sentence in a newspaper style, and keep the gold verification plan. Incorrect samples replace
the number with a common misreading (separate statements, cumulative figure, prior period, owners'
vs total net income, unit slip, near miss, wrong growth base/denominator, %p vs %, sign).

Labels are defined by the tolerance rule only (numbers.consistent), not by the verifier, so the
verifier can still be wrong in either direction. Each sample carries labels for both modes.

    python -m bench.generate --cache tests/fixtures/dart --offline --companies 00126380 --out bench/data/samsung.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

from finground_kr import numbers as N
from finground_kr.dart import DartClient
from finground_kr.facts import BASE_METRICS, FactStore, Period, Unavailable

METRIC_KO = {"revenue": "매출", "operating_income": "영업이익", "net_income": "당기순이익", "net_income_owners": "지배주주 순이익",
             "assets": "자산총계", "liabilities": "부채총계", "equity_total": "자본총계",
             "operating_margin": "영업이익률", "net_margin": "순이익률", "debt_ratio": "부채비율"}
LOSS_KO = {"operating_income": "영업손실", "net_income": "당기순손실", "net_income_owners": "지배주주 순손실"}
PART_KO = {"Q1": "1분기", "Q2": "2분기", "Q3": "3분기", "Q4": "4분기", "H1": "상반기", "FY": "연간"}
TAILS = ["으로 집계됐다", "을 기록했다", "이었다", "으로 나타났다", "을 올렸다"]
E8, E11, E12 = Decimal("1e8"), Decimal("1e11"), Decimal("1e12")


# ---------------------------------------------------------------------- rendering
def _q(v: Decimal, unit: Decimal, rounding) -> Decimal:
    return (v / unit).quantize(Decimal(1), rounding=rounding) * unit


def render_amount(v: Decimal, style: str) -> str | None:
    """Korean text for a won amount in one style, or None when the style does not fit the magnitude."""
    a = abs(v)
    if style in ("full_eok", "trunc_eok"):
        q = _q(a, E8, ROUND_HALF_UP if style == "full_eok" else ROUND_DOWN)
        if q < E8:
            return None
        jo, eok = divmod(q, E12)
        eok = int(eok / E8)
        text = (f"{int(jo):,}조 {eok:,}억원" if eok else f"{int(jo):,}조원") if jo else f"{eok:,}억원"
    elif style in ("cheon_eok", "trunc_cheon_eok"):
        if a < E12:
            return None
        q = _q(a, E11, ROUND_HALF_UP if style == "cheon_eok" else ROUND_DOWN)
        jo, rest = divmod(q, E12)
        cheon = int(rest / E11)
        text = f"{int(jo):,}조{cheon}천억원" if cheon else f"{int(jo):,}조원"
    elif style in ("jo_1dec", "approx_jo_1dec"):
        if a < E12:
            return None
        q = (a / E12).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        text = f"{q}조원"
        if style == "approx_jo_1dec":
            text = "약 " + text
    elif style == "eok_plain":
        q = (a / E8).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if q < 1:
            return None
        text = f"{int(q):,}억원"
    elif style == "baek_eok":
        q = _q(a, Decimal("1e10"), ROUND_HALF_UP)
        if q < Decimal("1e10"):
            return None
        jo, eok = divmod(q, E12)
        eok = int(eok / E8)
        text = (f"{int(jo):,}조 {eok:,}억원" if eok else f"{int(jo):,}조원") if jo else f"{eok:,}억원"
    else:
        raise ValueError(style)
    return text


def render_rate(v: Decimal, style: str, unit: str = "%") -> str:
    a = abs(v)
    if style == "pct_1dec":
        t = f"{a.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):,}{unit}"
    elif style == "pct_1dec_trunc":
        t = f"{a.quantize(Decimal('0.1'), rounding=ROUND_DOWN):,}{unit}"
    elif style == "pct_int":
        t = f"{a.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}{unit}"
    elif style == "pct_int_trunc":
        t = f"{a.quantize(Decimal(1), rounding=ROUND_DOWN):,}{unit}"
    elif style == "approx_pct_int":
        t = f"약 {a.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}{unit}"
    else:
        raise ValueError(style)
    return t


AMOUNT_STYLES = ("full_eok", "trunc_eok", "cheon_eok", "trunc_cheon_eok", "jo_1dec", "approx_jo_1dec", "eok_plain", "baek_eok")
RATE_STYLES = ("pct_1dec", "pct_1dec_trunc", "pct_int", "pct_int_trunc", "approx_pct_int")


def period_ko(p: Period) -> str:
    if p.scope == "point" and p.part != "FY":
        return f"{p.year}년 {PART_KO[p.part]} 말"
    if p.scope == "point":
        return f"{p.year}년 말"
    if p.scope == "cumulative" and p.part == "Q3":
        return f"{p.year}년 1~3분기 누적"
    return f"{p.year}년 {PART_KO[p.part]}"


# ---------------------------------------------------------------------- generation
class Generator:
    def __init__(self, store: FactStore, corp: dict, rng: random.Random) -> None:
        self.store, self.corp, self.rng = store, corp, rng
        self.name = corp["corp_name"]
        self.code = corp["corp_code"]
        self.samples: list[dict] = []
        self.skipped: dict[str, int] = {}

    # ----- helpers
    def _skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def _val(self, metric: str, period: Period, basis: str, prior: str | None = None):
        s = self.store
        try:
            if metric in ("operating_margin", "net_margin", "debt_ratio"):
                return s.derived(self.code, metric, period, basis, prior)
            if prior is None:
                return s.base(self.code, metric, period, basis)
            if prior == "yoy":
                return s.prior_year(self.code, metric, period, basis)
            if prior == "qoq":
                return s.prior_quarter(self.code, metric, period, basis)
            return s.prior_year_end(self.code, metric, period, basis)
        except Unavailable:
            return None

    def _plan(self, pid: str, quote: str, kind: str, metric: str, period: Period, basis_stated: bool, basis: str,
              mspan: str, pspan: str, value_span: str | None = None, change_span: str | None = None, compare: str | None = None) -> dict:
        p = {"id": pid, "quote": quote, "kind": kind, "company": {"name": self.name, "span": self.name},
             "metric": {"id": metric, "span": mspan}, "period": {"year": period.year, "part": period.part, "scope": period.scope, "span": pspan}}
        if basis_stated:
            p["basis"] = {"value": basis, "span": "별도" if basis == "OFS" else "연결"}
        if value_span:
            p["value"] = {"span": value_span}
        if change_span:
            p["change"] = {"span": change_span, "compare": compare}
        return p

    def _labels(self, text_q: N.Quantity, truth: Decimal) -> dict:
        return {m: ("correct" if N.consistent(text_q, truth, m)[0] else "incorrect") for m in N.MODES}

    def _emit(self, sid: str, sentence: str, as_of: str, plans: list[dict], truth: Decimal, unit: str, style: str,
              text: str, trap: dict | None, q: N.Quantity | None) -> None:
        if q is None:
            self._skip("parse")
            return
        labels = self._labels(q, truth)
        if trap and labels["default"] == "correct":  # the injected error is within tolerance: useless as a trap
            self._skip(f"trap within tolerance: {trap['type']}")
            return
        if not trap and labels["default"] == "incorrect":
            self._skip(f"rendering outside tolerance: {style}")
            return
        self.samples.append({"id": sid, "company": {"name": self.name, "corp_code": self.code, "stock_code": self.corp["stock_code"]},
                             "sentence": sentence, "as_of": as_of, "plans": plans, "truth": {"value": str(truth), "unit": unit},
                             "style": style, "text": text, "labels": labels, "trap": trap})

    # ----- value claims
    def value_claims(self, metric: str, period: Period, basis: str = "CFS") -> None:
        fact = self._val(metric, period, basis)
        if fact is None:
            return
        truth = fact.value
        as_of = fact.filed
        unit = fact.unit
        pko = period_ko(period)
        styles = AMOUNT_STYLES if unit == "KRW" else RATE_STYLES
        candidates: list[tuple[str, Decimal, dict | None]] = [("truth", truth, None)]
        # traps
        def add(kind, value, diag):
            if value is not None and value != truth:
                candidates.append((kind, value, {"type": kind, "expected_diagnosis": diag}))
        if unit == "KRW":
            other = "OFS" if basis == "CFS" else "CFS"
            f = self._val(metric, period, other)
            add("basis_swap", f.value if f else None, "별도 기준 값" if other == "OFS" else "연결 기준 값")
            if period.scope == "quarter" and period.part in ("Q2", "Q3"):
                f = self._val(metric, Period(period.year, period.part, "cumulative"), basis)
                add("cumulative_for_quarter", f.value if f else None, "누적 값")
            if period.scope == "cumulative" and period.part in ("H1", "Q3"):
                f = self._val(metric, Period(period.year, "Q2" if period.part == "H1" else "Q3", "quarter"), basis)
                add("quarter_for_cumulative", f.value if f else None, "3개월 값")
            f = self._val(metric, period, basis, "yoy")
            add("prior_year", f.value if f else None, "전년 동기 값")
            f = self._val(metric, period, basis, "qoq")
            add("prior_quarter", f.value if f else None, "직전 분기 값")
            if metric in ("net_income", "net_income_owners"):
                f = self._val("net_income_owners" if metric == "net_income" else "net_income", period, basis)
                add("owners_swap", f.value if f else None, "지배기업 소유주지분 순이익" if metric == "net_income" else "비지배지분 포함")
            add("unit_x10", truth * 10, "단위 착오")
            add("unit_div10", truth / 10, "단위 착오")
            add("unit_div100", truth / 100, "단위 착오")
            if truth > 0 and metric in LOSS_KO:
                candidates.append(("sign_flip", -truth, {"type": "sign_flip", "expected_diagnosis": "부호"}))
        for d in (Decimal("0.01"), Decimal("0.02"), Decimal("0.05")):
            add(f"near_miss_{d}", truth * (1 + d * self.rng.choice((1, -1))), None)
        for kind, value, trap in candidates:
            style = self.rng.choice(styles)
            if unit == "KRW":
                text = render_amount(value, style)
                if text is None:
                    text = render_amount(value, "full_eok")
                    style = "full_eok"
                if text is None:
                    self._skip("too small")
                    continue
                loss = value < 0 and metric in LOSS_KO
                if value < 0 and not loss:
                    text = "-" + text
                mko = LOSS_KO[metric] if loss else METRIC_KO[metric]
                q = N.parse_amount(text)
                if loss:
                    q = N.Quantity(-q.value, q.unit, q.ulp, q.ulp_lenient, q.bound, q.upper, q.text)
            else:
                text = ("-" if value < 0 else "") + render_rate(value, style, "%")
                mko = METRIC_KO[metric]
                q = N.parse_rate(text)
            tail = self.rng.choice(TAILS)
            sentence = f"{self.name}의 {pko} {mko}은 {text}{tail}"
            sid = f"{self.corp['stock_code']}-{metric}-{period.year}{period.part}{period.scope[0]}-{kind}"
            plan = self._plan(sid, sentence, "value", metric, period, False, basis, mko, pko, value_span=text)
            self._emit(sid, sentence, as_of, [plan], truth, unit, style, text, trap, q)

    # ----- change claims
    def change_claims(self, metric: str, period: Period, basis: str = "CFS") -> None:
        cur = self._val(metric, period, basis)
        prev = self._val(metric, period, basis, "yoy")
        if cur is None or prev is None:
            return
        as_of = max(x for x in (cur.filed, prev.filed) if x)
        pko = period_ko(period)
        c, p = cur.value, prev.value
        unit = cur.unit
        if unit == "KRW" and p > 0:
            truth = (c - p) / p * 100
            cands: list[tuple[str, Decimal, dict | None]] = [("truth", truth, None)]
            def add(kind, value, diag):
                if value is not None and value != truth:
                    cands.append((kind, value, {"type": kind, "expected_diagnosis": diag}))
            q = self._val(metric, period, basis, "qoq")
            if q is not None and q.value > 0:
                add("growth_qoq_base", (c - q.value) / q.value * 100, "직전 분기 대비로 계산한 값")
            if c > 0:
                add("growth_denominator_current", (c - p) / c * 100, "분모를 당기")
            if period.scope == "quarter" and period.part in ("Q2", "Q3"):
                cc = self._val(metric, Period(period.year, period.part, "cumulative"), basis)
                pc = self._val(metric, Period(period.year, period.part, "cumulative"), basis, "yoy")
                if cc and pc and pc.value > 0:
                    add("growth_cumulative", (cc.value - pc.value) / pc.value * 100, "누적 값")
            add("growth_sign", -truth, "부호")
            for d in (Decimal("0.02"), Decimal("0.05")):
                add(f"growth_near_miss_{d}", truth * (1 + d * self.rng.choice((1, -1))), None)
            for kind, value, trap in cands:
                style = self.rng.choice(RATE_STYLES)
                text = render_rate(value, style, "%")
                word = "증가" if value > 0 else "감소"
                change = f"전년 동기 대비 {text} {word}"
                sentence = f"{self.name}의 {pko} {METRIC_KO[metric]}은 {change}했다"
                sid = f"{self.corp['stock_code']}-{metric}-{period.year}{period.part}{period.scope[0]}-{kind}"
                plan = self._plan(sid, sentence, "change", metric, period, False, basis, METRIC_KO[metric], pko, change_span=change, compare="yoy")
                self._emit(sid, sentence, as_of, [plan], truth, "%", style, text, trap, N.parse_change(change).quantity)
            # amount difference
            diff = c - p
            style = self.rng.choice(("full_eok", "cheon_eok", "jo_1dec"))
            text = render_amount(diff, style) or render_amount(diff, "full_eok")
            if text:
                change = f"전년 동기보다 {text} {'늘었' if diff > 0 else '줄었'}다"
                sentence = f"{self.name}의 {pko} {METRIC_KO[metric]}은 {change}"
                sid = f"{self.corp['stock_code']}-{metric}-{period.year}{period.part}{period.scope[0]}-diff"
                plan = self._plan(sid, sentence, "change", metric, period, False, basis, METRIC_KO[metric], pko, change_span=change, compare="yoy")
                self._emit(sid, sentence, as_of, [plan], diff, "KRW", style, text, None, N.parse_change(change).quantity)
        if unit == "%":  # margins: %p change, and the %-instead-of-%p trap
            truth = c - p
            cands = [("truth", truth, None)]
            if p != 0:
                cands.append(("pp_as_pct", (c - p) / abs(p) * 100, {"type": "pp_as_pct", "expected_diagnosis": "%p가 아니라"}))
            for kind, value, trap in cands:
                style = self.rng.choice(("pct_1dec", "pct_1dec_trunc"))
                text = render_rate(value, style, "%p" if kind == "truth" else "%")
                word = "상승" if value > 0 else "하락"
                change = f"전년 동기 대비 {text} {word}"
                sentence = f"{self.name}의 {pko} {METRIC_KO[metric]}은 {change}했다"
                sid = f"{self.corp['stock_code']}-{metric}-{period.year}{period.part}{period.scope[0]}-{kind}"
                plan = self._plan(sid, sentence, "change", metric, period, False, basis, METRIC_KO[metric], pko, change_span=change, compare="yoy")
                # a %-growth claim about a margin is a pp claim written in the wrong unit; label it against the %p truth
                self._emit(sid, sentence, as_of, [plan], truth, "%p", style, text, trap, N.parse_change(change).quantity)

    # ----- combined sentence (value + growth) for the planner experiment
    def combined(self, metric: str, period: Period, basis: str = "CFS") -> None:
        cur, prev = self._val(metric, period, basis), self._val(metric, period, basis, "yoy")
        if cur is None or prev is None or prev.value <= 0 or cur.unit != "KRW":
            return
        text = render_amount(cur.value, "full_eok")
        g = (cur.value - prev.value) / prev.value * 100
        gtext = render_rate(g, "pct_1dec")
        change = f"전년 동기 대비 {gtext} {'증가' if g > 0 else '감소'}"
        pko = period_ko(period)
        sentence = f"{self.name}의 {pko} {METRIC_KO[metric]}은 {text}으로 {change}했다"
        base = f"{self.corp['stock_code']}-{metric}-{period.year}{period.part}{period.scope[0]}-combined"
        plans = [self._plan(base + "-v", sentence, "value", metric, period, False, basis, METRIC_KO[metric], pko, value_span=text),
                 self._plan(base + "-c", sentence, "change", metric, period, False, basis, METRIC_KO[metric], pko, change_span=change, compare="yoy")]
        q = N.parse_amount(text)
        self.samples.append({"id": base, "company": {"name": self.name, "corp_code": self.code, "stock_code": self.corp["stock_code"]},
                             "sentence": sentence, "as_of": max(x for x in (cur.filed, prev.filed) if x), "plans": plans,
                             "truth": {"value": str(cur.value), "unit": "KRW", "growth": str(g)}, "style": "combined", "text": text,
                             "labels": self._labels(q, cur.value), "trap": None})

    def run(self, years: list[int]) -> None:
        flows = ["revenue", "operating_income", "net_income", "net_income_owners"]
        points = ["assets", "liabilities", "equity_total"]
        for y in years:
            for part in ("Q1", "Q2", "Q3", "Q4"):
                per = Period(y, part, "quarter")
                for m in flows:
                    self.value_claims(m, per)
                    self.change_claims(m, per)
                for m in ("operating_margin", "net_margin"):
                    self.value_claims(m, per)
                    self.change_claims(m, per)
                for m in points:
                    self.value_claims(m, Period(y, part, "point"))
                self.value_claims("debt_ratio", Period(y, part, "quarter"))
                self.combined("operating_income", per)
            for per in (Period(y, "H1", "cumulative"), Period(y, "Q3", "cumulative"), Period(y, "FY", "annual")):
                for m in flows:
                    self.value_claims(m, per)
                    self.change_claims(m, per)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--companies", nargs="+", required=True, help="corp codes")
    ap.add_argument("--years", nargs="+", type=int, default=[2025, 2026])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    client = DartClient(args.cache, offline=args.offline)
    store = FactStore(client)
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(out, "w", encoding="utf-8") as f:
        for code in args.companies:
            hits = [c for c in client.corp_codes() if c["corp_code"] == code]
            if not hits:
                print(f"skip {code}: not in corp codes")
                continue
            g = Generator(store, hits[0], rng)
            g.run(args.years)
            for s in g.samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
            total += len(g.samples)
            traps = sum(1 for s in g.samples if s["trap"])
            print(f"{hits[0]['corp_name']}: {len(g.samples)} samples ({traps} with traps); skipped {g.skipped}")
    print(f"wrote {total} samples to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
