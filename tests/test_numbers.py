from decimal import Decimal as D

import pytest

from finground_kr.numbers import ParseError, Quantity, consistent, fmt_krw, parse_amount, parse_change, parse_rate

AMOUNTS = {
    "10조4천억원": (D("10.4e12"), D("1e11"), D("1e11"), "eq"),
    "10조 4,400억원": (D("10.44e12"), D("1e8"), D("1e10"), "eq"),
    "1,234백만원": (D("1.234e9"), D("1e6"), D("1e6"), "eq"),
    "3천만원": (D("3e7"), D("1e7"), D("1e7"), "eq"),
    "5억 3천만원": (D("5.3e8"), D("1e7"), D("1e7"), "eq"),
    "1.5조원": (D("1.5e12"), D("1e11"), D("1e11"), "eq"),
    "1천2백억원": (D("1.2e11"), D("1e10"), D("1e10"), "eq"),
    "100억원": (D("1e10"), D("1e8"), D("1e9"), "eq"),
    "10조원": (D("1e13"), D("1e12"), D("1e12"), "eq"),
    "-1,200억원": (D("-1.2e11"), D("1e8"), D("1e10"), "eq"),
    "△450억원": (D("-4.5e10"), D("1e8"), D("1e9"), "eq"),
    "약 10조원": (D("1e13"), D("1e12"), D("1e12"), "approx"),
    "10조원 수준": (D("1e13"), D("1e12"), D("1e12"), "approx"),
    "10조원대": (D("1e13"), D("1e12"), D("1e12"), "range"),
    "1조원 돌파": (D("1e12"), D("1e12"), D("1e12"), "ge"),
    "1조원을 넘어": (D("1e12"), D("1e12"), D("1e12"), "gt"),
    "1조여원": (D("1e12"), D("1e12"), D("1e12"), "above"),
    "1조원에 육박": (D("1e12"), D("1e12"), D("1e12"), "below"),
    "매출 305조 3,729억원을 기록": (D("305.3729e12"), D("1e8"), D("1e8"), "eq"),
    "계약 금액 100억원": (D("1e10"), D("1e8"), D("1e9"), "eq"),  # '약' inside 계약 is not a hedge
    "1,234원": (D("1234"), D("1"), D("1"), "eq"),
}


@pytest.mark.parametrize("text,expected", AMOUNTS.items())
def test_parse_amount(text, expected):
    q = parse_amount(text)
    assert (q.value, q.ulp, q.ulp_lenient, q.bound) == expected and q.unit == "KRW"


def test_range_and_errors():
    assert parse_amount("10조원대").upper == D("2e13")
    assert parse_amount("1,500억원대").upper == D("1.6e11")
    for bad in ("1,234", "매출이 늘었다", "12%"):
        with pytest.raises(ParseError):
            parse_amount(bad)


@pytest.mark.parametrize("text,value,unit,ulp,bound", [
    ("8.3%", D("8.3"), "%", D("0.1"), "eq"), ("15% 증가", D("15"), "%", D("1"), "eq"), ("2.1%p", D("2.1"), "%p", D("0.1"), "eq"),
    ("1.5%포인트 상승", D("1.5"), "%p", D("0.1"), "eq"), ("약 8%", D("8"), "%", D("1"), "approx"), ("-3.2%", D("-3.2"), "%", D("0.1"), "eq"),
    ("1,813%", D("1813"), "%", D("1"), "eq"),
])
def test_parse_rate(text, value, unit, ulp, bound):
    q = parse_rate(text)
    assert (q.value, q.unit, q.ulp, q.bound) == (value, unit, ulp, bound)


@pytest.mark.parametrize("text,kind,direction,value", [
    ("전년 동기 대비 15% 증가", "growth_pct", 1, D("15")), ("2.1%p 하락", "pp", -1, D("-2.1")), ("1,200억원 감소", "diff", -1, D("-1.2e11")),
    ("흑자 전환", "turn_profit", 0, None), ("적자 폭 축소", "loss_narrow", 0, None), ("소폭 증가", "direction", 1, None),
    ("전년 대비 12.5% 줄었다", "growth_pct", -1, D("-12.5")), ("보합", "flat", 0, None), ("적자를 이어갔다", "stay_loss", 0, None),
])
def test_parse_change(text, kind, direction, value):
    c = parse_change(text)
    assert (c.kind, c.direction, c.quantity.value if c.quantity else None) == (kind, direction, value)


def test_parse_change_rejects_conflicts():
    with pytest.raises(ParseError):
        parse_change("증가했다가 감소")
    with pytest.raises(ParseError):
        parse_change("그대로였다")


def test_tolerance_modes():
    q = parse_amount("10조 4,400억원")  # strict precision 1억, lenient 100억
    cases = {D("10.44e12"): ("exact", "exact"), D("10.4412e12"): ("round", "mismatch"), D("10.4489e12"): ("trunc", "mismatch"),
             D("10.4499e12"): ("trunc", "mismatch"), D("10.4501e12"): ("mismatch", "mismatch"), D("10.4389e12"): ("round", "mismatch"),
             D("10.44000004e12"): ("round", "round")}
    for true, (default, strict) in cases.items():
        assert consistent(q, true, "default")[1] == default, true
        assert consistent(q, true, "strict")[1] == strict, true
    neg = parse_amount("-4,400억원")
    assert consistent(neg, D("-4.489e11"))[1] == "trunc" and consistent(neg, D("-4.311e11"))[1] == "mismatch"
    assert consistent(parse_amount("약 89조원"), D("89.49e12"))[1] == "approx"
    assert consistent(parse_amount("89조원대"), D("89.49e12"))[1] == "range"
    assert not consistent(parse_amount("89조원대"), D("90.1e12"))[0]
    assert consistent(parse_amount("1조원 돌파"), D("1.2e12"))[0] and not consistent(parse_amount("1조원 돌파"), D("0.99e12"))[0]
    with pytest.raises(ValueError):
        consistent(q, D(1), "loose")


def test_format():
    assert fmt_krw(D("89492412000000")) == "89조 4,924억원"
    assert fmt_krw(D("-120000000000")) == "-1,200억원"
    assert fmt_krw(D("35000000")) == "3,500만원"
    assert fmt_krw(D("1234")) == "1,234원"
