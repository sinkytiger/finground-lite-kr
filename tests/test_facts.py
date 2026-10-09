from decimal import Decimal as D

import pytest

from finground_kr.dart import DartError
from finground_kr.facts import FactStore, Period, Unavailable, metric_id, normalize_period

SAMSUNG = "00126380"
M = D("1e6")  # DART amounts are in won; the recorded figures below are in 백만원


def test_metric_aliases():
    assert metric_id("영업이익") == "operating_income" and metric_id("영업손실") == "operating_income"
    assert metric_id("지배주주 순이익") == "net_income_owners" and metric_id("순이익") == "net_income"
    assert metric_id("operating_margin") == "operating_margin" and metric_id("주가") is None


def test_period_normalization():
    assert normalize_period(2026, "Q2", None) == Period(2026, "Q2", "quarter")
    assert normalize_period(2026, "H1", None) == Period(2026, "H1", "cumulative")
    assert normalize_period(2025, "FY", None) == Period(2025, "FY", "annual")
    assert normalize_period(2025, "FY", "point") == Period(2025, "FY", "point")
    assert Period(2026, "Q1", "quarter").prior_quarter() == Period(2025, "Q4", "quarter")
    assert Period(2026, "H1", "cumulative").prior_quarter() is None
    with pytest.raises(ValueError):
        normalize_period(2026, "Q2", "weekly")


def test_quarter_cumulative_annual_and_point(client):
    s = FactStore(client)
    assert s.base(SAMSUNG, "operating_income", Period(2026, "Q2", "quarter"), "CFS").value == 89_492_412 * M
    assert s.base(SAMSUNG, "operating_income", Period(2026, "H1", "cumulative"), "CFS").value == 146_725_209 * M
    assert s.base(SAMSUNG, "operating_income", Period(2026, "Q1", "quarter"), "CFS").value == 57_232_797 * M
    assert s.base(SAMSUNG, "revenue", Period(2025, "FY", "annual"), "CFS").value == 333_605_938 * M
    assert s.base(SAMSUNG, "equity_total", Period(2026, "Q2", "point"), "CFS").value == 579_309_676 * M
    assert s.base(SAMSUNG, "operating_income", Period(2026, "Q2", "quarter"), "OFS").value == 85_410_711 * M


def test_q4_is_derived_from_two_reports(client):
    f = FactStore(client).base(SAMSUNG, "operating_income", Period(2025, "Q4", "quarter"), "CFS")
    assert f.value == (43_601_051 - 23_527_391) * M
    assert [(src.report.period, src.column) for src in f.sources] == [("FY", "thstrm"), ("Q3", "thstrm_add")]
    assert "4분기" in f.derivation


def test_prior_year_uses_comparative_columns(client):
    s = FactStore(client)
    p = s.prior_year(SAMSUNG, "operating_income", Period(2026, "Q2", "quarter"), "CFS")
    assert p.value == 4_676_057 * M and p.sources[0].column == "frmtrm_q" and p.period == Period(2025, "Q2", "quarter")
    p = s.prior_year(SAMSUNG, "revenue", Period(2026, "H1", "cumulative"), "CFS")
    assert p.value == 153_706_820 * M and p.sources[0].column == "frmtrm_add"
    p = s.prior_year(SAMSUNG, "revenue", Period(2025, "FY", "annual"), "CFS")
    assert p.value == 300_870_903 * M and p.sources[0].column == "frmtrm"
    ye = s.prior_year_end(SAMSUNG, "equity_total", Period(2026, "Q2", "point"), "CFS")
    assert ye.value == 436_320_337 * M and ye.period == Period(2025, "FY", "point")
    # balance sheet, same quarter last year: a separate report, not the comparative column
    q = s.prior_year(SAMSUNG, "equity_total", Period(2026, "Q2", "point"), "CFS")
    assert q.sources[0].report.year == 2025 and q.sources[0].report.period == "H1"


def test_prior_quarter_and_ttm(client):
    s = FactStore(client)
    assert s.prior_quarter(SAMSUNG, "operating_income", Period(2026, "Q1", "quarter"), "CFS").value == (43_601_051 - 23_527_391) * M
    ttm = s.ttm(SAMSUNG, "operating_income", Period(2026, "Q2", "quarter"), "CFS")
    assert ttm.value == (43_601_051 - 11_361_329 + 146_725_209) * M
    assert len(ttm.sources) == 3


def test_derived_margin(client):
    s = FactStore(client)
    f = s.derived(SAMSUNG, "operating_margin", Period(2026, "Q2", "quarter"), "CFS")
    assert f.unit == "%" and abs(f.value - D(89_492_412) / D(171_499_470) * 100) < D("1e-9")
    prev = s.derived(SAMSUNG, "operating_margin", Period(2026, "Q2", "quarter"), "CFS", prior="yoy")
    assert abs(prev.value - D(4_676_057) / D(74_566_317) * 100) < D("1e-9")


def test_as_of_hides_later_reports(client):
    s = FactStore(client, as_of="2026-08-01")
    with pytest.raises(Unavailable, match="2026-08-14"):
        s.base(SAMSUNG, "operating_income", Period(2026, "Q2", "quarter"), "CFS")
    assert FactStore(client, as_of="2026-08-14").base(SAMSUNG, "operating_income", Period(2026, "Q2", "quarter"), "CFS")


def test_missing_report_and_offline(client):
    with pytest.raises(Unavailable):
        FactStore(client).base(SAMSUNG, "operating_income", Period(2027, "Q1", "quarter"), "CFS")
    with pytest.raises(DartError, match="offline"):
        client.report("00000009", 2026, "Q1", "CFS")
    assert client.resolve("삼성전자")[0]["stock_code"] == "005930" == client.resolve(" 삼성 전자 ")[0]["stock_code"]
    assert len(client.resolve("샘플전자")) == 2 and client.resolve("없는회사") == []
