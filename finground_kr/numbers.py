"""Korean financial number expressions -> exact quantities with their written precision.

    "10조4천억원"      -> 10.4e12 KRW, last written digit = 1천억
    "10조 4,400억원"   -> 10.44e12 KRW, strict precision 1억, lenient precision 100억
    "1,234백만원"      -> 1.234e9 KRW (DART tables are often in 백만원)
    "약 8.3%"          -> 8.3 %, approximate
    "10조원대"         -> range [10조, 20조)
    "2.1%p 상승"       -> +2.1 %p

Precision follows VeriFin (arXiv 2608.10213): a claim is consistent with a filed value when they
differ by at most half a unit in the last place the claim writes. Korean news often truncates
("4,400억" for 4,412억), so the default mode also accepts truncation and treats trailing zeros of
the last digit group as non-significant (keeping at least two significant digits).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

BIG = {"조": Decimal(10) ** 12, "억": Decimal(10) ** 8, "만": Decimal(10) ** 4}
SMALL = {"천": Decimal(1000), "백": Decimal(100), "십": Decimal(10)}
NEG = "-−△▲"
MODES = ("default", "strict")

LOSS_WORDS = ("손실", "적자")
APPROX_AFTER = ("가량", "정도", "수준", "안팎", "내외", "쯤", "어림", "남짓")
ABOVE = ("남짓",)                                   # "1조원 남짓": a little above
BELOW = ("육박", "가까이", "근접", "가까운", "턱밑")
LOWER = (("돌파", "ge"), ("이상", "ge"), ("넘", "gt"), ("초과", "gt"), ("웃돌", "gt"), ("상회", "gt"))
UPPER = (("이하", "le"), ("미만", "lt"), ("밑돌", "lt"), ("못미", "lt"), ("하회", "lt"))


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Quantity:
    value: Decimal                 # KRW, % or %p
    unit: str                      # "KRW" | "%" | "%p"
    ulp: Decimal                   # place value of the last written digit
    ulp_lenient: Decimal           # same, with trailing zeros of the last group treated as non-significant
    bound: str = "eq"              # eq | approx | above | below | ge | gt | le | lt | range
    upper: Decimal | None = None   # upper end for bound == "range"
    text: str = ""

    def unit_for(self, mode: str) -> Decimal:
        return self.ulp if mode == "strict" else self.ulp_lenient


def consistent(q: Quantity, true: Decimal, mode: str = "default") -> tuple[bool, str]:
    """Is the filed/computed value `true` consistent with the written quantity?

    Returns (ok, how). how: exact | round | trunc | approx | bound | range | mismatch.
    strict  = rounding at the written precision only (paper style)
    default = rounding or truncation, lenient precision
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    v, u = q.value, q.unit_for(mode)
    if q.bound == "range":
        return (v <= true < q.upper), "range"
    if q.bound in ("ge", "gt", "le", "lt"):
        ok = {"ge": true >= v, "gt": true > v, "le": true <= v, "lt": true < v}[q.bound]
        return ok, "bound"
    if q.bound == "approx":
        return (v - u <= true <= v + u), "approx"
    if q.bound == "above":
        return (v <= true <= v + u), "approx"
    if q.bound == "below":
        return (v - u <= true <= v), "approx"
    if true == v:
        return True, "exact"
    if abs(true - v) <= u / 2:
        return True, "round"
    if mode == "default":
        # truncation toward zero: written 4,400억 for a true 4,489억 (or -4,400억 for -4,489억)
        if (v >= 0 and v <= true < v + u) or (v < 0 and v - u < true <= v):
            return True, "trunc"
    return False, "mismatch"


# ---------------------------------------------------------------------- helpers
def _digits(tok: str) -> tuple[int, int, int]:
    """(decimal places, integer digits, trailing zeros of the integer part) of a numeric token."""
    whole, _, frac = tok.replace(",", "").partition(".")
    whole = whole.lstrip("0") or "0"
    trailing = 0 if whole == "0" else len(whole) - len(whole.rstrip("0"))
    return len(frac), len(whole), trailing


def _precision(tok: str, mult: Decimal) -> tuple[Decimal, Decimal]:
    decimals, ndigits, trailing = _digits(tok)
    ulp = mult * (Decimal(10) ** -decimals)
    if decimals == 0 and trailing:
        return ulp, mult * (Decimal(10) ** min(trailing, max(ndigits - 2, 0)))
    return ulp, ulp


def _bound(before: str, after: str, tok: str, mult: Decimal, value: Decimal) -> tuple[str, Decimal | None]:
    """Hedges and bounds from the text around the number (whitespace removed)."""
    if re.match(r"[%원조억만]*대(?!비|로|상|조|비해)", after):
        decimals, _, trailing = _digits(tok)
        width = mult * (Decimal(10) ** trailing) * (Decimal(10) ** -decimals)
        return "range", value + width
    if re.match(r"(?:원|%)?여", after):
        return "above", None
    if any(w in after for w in ABOVE):
        return "above", None
    if any(w in after for w in BELOW):
        return "below", None
    for word, kind in LOWER + UPPER:
        if word in after:
            return kind, None
    if re.search(r"(?:^|[^가-힣])약$", before) or any(w in after for w in APPROX_AFTER):
        return "approx", None
    return "eq", None


def is_loss(span) -> bool:
    return any(w in str(span or "") for w in LOSS_WORDS)


# ---------------------------------------------------------------------- amounts
_AMOUNT_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?|[조억만천백십]")


def parse_amount(span: str) -> Quantity:
    """Parse the first Korean won amount in `span`, e.g. "약 10조 4,400억원 수준"."""
    compact = re.sub(r"\s+", "", span)
    m = re.search(r"[" + NEG + r"]?\d", compact)
    if not m:
        raise ParseError(f"no number in {span!r}")
    i = m.start()
    sign = Decimal(1)
    if compact[i] in NEG:
        sign = Decimal(-1)
        i += 1
    j = i
    while j < len(compact) and (compact[j].isdigit() or compact[j] in ",." or compact[j] in BIG or compact[j] in SMALL):
        j += 1
    core = compact[i:j].rstrip(",.")
    after = compact[i + len(core):]
    before = compact[:m.start()]
    if not (after.startswith("원") or any(u in core for u in BIG) or core[-1:] in SMALL):
        raise ParseError(f"{span!r} has no won unit (원/만/억/조)")

    total = group = Decimal(0)
    n: Decimal | None = None
    n_tok = ""
    last_tok, last_mult = "", Decimal(1)
    for tok in _AMOUNT_TOKEN.findall(core):
        if tok[0].isdigit():
            n, n_tok = Decimal(tok.replace(",", "")), tok
        elif tok in SMALL:
            if n is None:
                n, n_tok = Decimal(1), "1"
            group += n * SMALL[tok]
            last_tok, last_mult = n_tok, SMALL[tok]
            n = None
        else:  # BIG
            if n is not None:
                group += n
                last_tok, last_mult = n_tok, Decimal(1)
                n = None
            if group == 0:
                group, last_tok, last_mult = Decimal(1), "1", Decimal(1)
            total += group * BIG[tok]
            last_mult *= BIG[tok]
            group = Decimal(0)
    if n is not None:
        group += n
        last_tok, last_mult = n_tok, Decimal(1)
    total += group
    if not last_tok:
        raise ParseError(f"cannot read the digits of {span!r}")

    value = sign * total
    ulp, ulp_len = _precision(last_tok, last_mult)
    bound, upper = _bound(before, after, last_tok, last_mult, value)
    return Quantity(value, "KRW", ulp, ulp_len, bound, upper, span)


# ---------------------------------------------------------------------- rates
_RATE = re.compile(r"([" + NEG + r"]?\d[\d,]*(?:\.\d+)?)(%p|%포인트|퍼센트포인트|pp|%|퍼센트)", re.IGNORECASE)


def parse_rate(span: str) -> Quantity:
    """Parse "8.3%", "15% 증가", "2.1%p", "1.5%포인트". Direction words are handled by parse_change."""
    compact = re.sub(r"\s+", "", span)
    m = _RATE.search(compact)
    if not m:
        raise ParseError(f"no percentage in {span!r}")
    num, unit_word = m.group(1), m.group(2).lower()
    sign = Decimal(-1) if num[0] in NEG else Decimal(1)
    num = num.lstrip(NEG)
    unit = "%p" if unit_word in ("%p", "%포인트", "퍼센트포인트", "pp") else "%"
    value = sign * Decimal(num.replace(",", ""))
    ulp, ulp_len = _precision(num, Decimal(1))
    bound, upper = _bound(compact[:m.start()], compact[m.end():], num, Decimal(1), value)
    return Quantity(value, unit, ulp, ulp_len, bound, upper, span)


# ---------------------------------------------------------------------- changes
INCREASE = ("증가", "늘", "상승", "성장", "급증", "급등", "확대", "개선", "뛰", "불어", "올라", "오른", "신장")
DECREASE = ("감소", "줄", "하락", "급감", "축소", "악화", "뒷걸음", "역성장", "떨어", "내려", "낮아", "위축")
TURNS = (("흑자전환", "turn_profit"), ("흑자로전환", "turn_profit"), ("흑전", "turn_profit"),
         ("적자전환", "turn_loss"), ("적자로전환", "turn_loss"), ("적전", "turn_loss"),
         ("적자폭축소", "loss_narrow"), ("적자축소", "loss_narrow"), ("손실폭축소", "loss_narrow"), ("적자폭을줄", "loss_narrow"),
         ("적자폭확대", "loss_widen"), ("적자확대", "loss_widen"), ("손실폭확대", "loss_widen"), ("적자폭이커", "loss_widen"),
         ("적자지속", "stay_loss"), ("적자를지속", "stay_loss"), ("적자가지속", "stay_loss"), ("적자를이어", "stay_loss"),
         ("흑자지속", "stay_profit"), ("흑자를지속", "stay_profit"), ("흑자를이어", "stay_profit"))
FLAT = ("보합", "변동없", "변화없", "동일", "같은수준", "제자리")


@dataclass(frozen=True)
class Change:
    kind: str                   # growth_pct | pp | diff | direction | flat | turn_profit | turn_loss | loss_narrow | loss_widen | stay_loss | stay_profit
    direction: int              # +1 increase, -1 decrease, 0 none/unknown
    quantity: Quantity | None   # signed magnitude for growth_pct / pp / diff
    text: str = ""


def parse_change(span: str) -> Change:
    """Parse "전년 동기 대비 15% 증가", "2.1%p 하락", "1,200억원 감소", "흑자 전환", "소폭 증가"."""
    compact = re.sub(r"\s+", "", span)
    for word, kind in TURNS:
        if word in compact:
            return Change(kind, 0, None, span)
    if any(w in compact for w in FLAT):
        return Change("flat", 0, None, span)
    up = any(w in compact for w in INCREASE)
    down = any(w in compact for w in DECREASE)
    if up and down:
        raise ParseError(f"both increase and decrease words in {span!r}")
    direction = 1 if up else -1 if down else 0
    q: Quantity | None = None
    kind = "direction"
    if _RATE.search(compact):
        q = parse_rate(span)
        kind = "pp" if q.unit == "%p" else "growth_pct"
    else:
        try:
            q = parse_amount(span)
            kind = "diff"
        except ParseError:
            q = None
    if q is None and direction == 0:
        raise ParseError(f"no change expression in {span!r}")
    if q is not None and direction:
        if q.value < 0 and direction > 0:
            raise ParseError(f"negative number with an increase word in {span!r}")
        q = Quantity(abs(q.value) * direction, q.unit, q.ulp, q.ulp_lenient, q.bound,
                     None if q.upper is None else abs(q.upper) * direction, q.text)
    return Change(kind, direction, q, span)


# ---------------------------------------------------------------------- display
def fmt_krw(value) -> str:
    """Newspaper style: 89조 4,924억원 / 4,912억원 / 3,500만원 / -1,200억원."""
    v = Decimal(value)
    neg = v < 0
    v = abs(v)
    jo, rest = divmod(v, BIG["조"])
    eok, rest = divmod(rest, BIG["억"])
    man, rest = divmod(rest, BIG["만"])
    parts = []
    if jo:
        parts.append(f"{int(jo):,}조")
    if eok:
        parts.append(f"{int(eok):,}억")
    if not jo and man:
        parts.append(f"{int(man):,}만")
    if not parts:
        parts.append(f"{int(rest):,}")
    return ("-" if neg else "") + " ".join(parts) + "원"


def fmt_rate(value, unit: str = "%") -> str:
    return f"{Decimal(value):,.2f}{unit}"
