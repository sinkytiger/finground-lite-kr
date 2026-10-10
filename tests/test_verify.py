import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from finground_kr.mcp_server import render_markdown
from finground_kr.verify import Verifier, read_period_span

from conftest import FIXTURES, plan

AS_OF = "2026-10-09"


def run(client, p, mode="default", as_of=AS_OF):
    return Verifier(client, mode).verify(p, as_of).as_dict()


def test_supported_value_and_citation(client):
    r = run(client, plan("a", "삼성전자의 2분기 영업이익은 89조4,924억원이었다", value="89조4,924억원", pspan="2분기"))
    assert r["status"] == "supported" and r["status_strict"] == "supported" and r["tolerance"]["how"] == "round"
    assert r["citations"][0]["rcept_no"] == "20260814003699" and r["citations"][0]["column"] == "thstrm"
    assert any("연결 기준" in n for n in r["notes"])


@pytest.mark.parametrize("value,metric,expect_diag", [
    ("146조7,252억원", "operating_income", "상반기 누적"),
    ("8,949억원", "operating_income", "단위 착오"),
    ("71조2,695억원", "net_income", "지배기업 소유주지분 순이익"),
    ("4조6,760억원", "operating_income", "전년 동기 값"),
    ("57조2,328억원", "operating_income", "직전 분기 값"),
    ("85조4,107억원", "operating_income", "별도 기준 값"),
])
def test_contradicted_with_trap_diagnosis(client, value, metric, expect_diag):
    r = run(client, plan("x", f"삼성전자의 2분기 영업이익은 {value}이었다", metric=metric, value=value, pspan="2분기"))
    assert r["status"] == "contradicted"
    assert any(expect_diag in d["interpretation"] for d in r["diagnosis"]), r["diagnosis"]


def test_sign_flip_diagnosis(client):
    r = run(client, plan("v", "삼성전자 2분기 영업손실은 89조4,924억원", value="89조4,924억원", pspan="2분기", mspan="영업손실"))
    assert r["status"] == "contradicted" and r["claim"]["value"].startswith("-")
    assert any("부호" in d["interpretation"] for d in r["diagnosis"])


def test_truncation_only_in_default_mode(client):
    p = plan("q", "삼성전자 2분기 영업이익은 89조4,900억원", value="89조4,900억원", pspan="2분기")
    assert run(client, p)["status"] == "supported" and run(client, p, "strict")["status"] == "contradicted"
    g = plan("d", "삼성전자 2분기 영업이익은 전년 동기 대비 1,813% 증가했다", kind="change", change="전년 동기 대비 1,813% 증가", pspan="2분기")
    r = run(client, g)
    assert r["status"] == "supported" and r["tolerance"]["how"] == "trunc" and r["status_strict"] == "contradicted"


def test_change_claims(client):
    pp = plan("u", "삼성전자 2분기 영업이익률은 전년 동기 대비 45.9%p 상승", kind="change", metric="operating_margin",
              change="전년 동기 대비 45.9%p 상승", pspan="2분기")
    assert run(client, pp)["status"] == "supported"
    diff = plan("s", "삼성전자 2분기 영업이익은 전년 동기보다 84조8,164억원 늘었다", kind="change", change="전년 동기보다 84조8,164억원 늘었다", pspan="2분기")
    assert run(client, diff)["status"] == "supported"
    qoq = plan("x", "삼성전자 2분기 영업이익은 전 분기 대비 56.4% 증가", kind="change", change="전 분기 대비 56.4% 증가", compare="qoq", pspan="2분기")
    r = run(client, qoq)
    assert r["status"] == "supported" and r["expected"]["previous_period"] == "2026년 1분기"
    wrong = plan("w", "삼성전자 2분기 영업이익은 전년 동기 대비 56.4% 증가", kind="change", change="전년 동기 대비 56.4% 증가", pspan="2분기")
    r = run(client, wrong)
    assert r["status"] == "contradicted" and any("직전 분기" in d["interpretation"] for d in r["diagnosis"])
    turn = plan("n", "삼성전자 2분기 순이익이 흑자 전환했다", kind="change", metric="net_income", change="흑자 전환", pspan="2분기")
    assert run(client, turn)["status"] == "contradicted"
    up = plan("m", "삼성전자 2분기 영업이익이 전년 동기보다 늘었다", kind="direction", change="전년 동기보다 늘었다", pspan="2분기")
    assert run(client, up)["status"] == "supported"


def test_q4_and_annual_and_ratio(client):
    q4 = plan("k", "삼성전자 지난해 4분기 영업이익은 20조737억원", year=2025, part="Q4", value="20조737억원", pspan="4분기")
    r = run(client, q4)
    assert r["status"] == "supported" and {c["report"] for c in r["citations"]} == {"2025년 사업보고서 (연결)", "2025년 3분기보고서 (연결)"}
    fy = plan("w", "삼성전자 2025년 매출은 333조6,059억원", metric="revenue", year=2025, part="FY", value="333조6,059억원", pspan="2025년")
    assert run(client, fy)["status"] == "supported"
    debt = plan("r", "삼성전자 2분기 말 부채비율은 31.1%", metric="debt_ratio", value="31.1%", pspan="2분기 말")
    assert run(client, debt)["status"] == "supported"
    margin = plan("e", "삼성전자 2분기 영업이익률은 52.2%", metric="operating_margin", value="52.2%", pspan="2분기")
    assert run(client, margin)["status"] == "supported"


@pytest.mark.parametrize("p,reason", [
    (plan("f", "삼성전자의 2분기 영업이익은 89조4,924억원", value="89조4,924억원", pspan="2분기"), "기준일"),
    (plan("g", "삼성전자 상반기 영업이익은 89조4,924억원", value="89조4,924억원", pspan="상반기", scope="quarter"), "H1인데 계획은 Q2"),
    (plan("t", "넥스트전자 2분기 영업이익은 1조원", value="1조원", pspan="2분기", cname="넥스트전자", cspan="넥스트전자"), "확정하지 못했습니다"),
    (plan("y", "2분기 영업이익은 1조원", value="1조원", pspan="2분기"), "원문에 없습니다"),
    (plan("z", "삼성전자 2분기 주가는 10만원", metric="주가", value="10만원", pspan="2분기"), "지원 범위 밖"),
    (plan("h", "삼성전자 하반기 영업이익은 1조원", part="Q3", value="1조원", pspan="하반기"), "하반기"),
    (plan("g2", "삼성전자 2분기 순이익은 전년 동기 대비 10% 증가", kind="change", metric="net_income", change="전년 동기 대비 10% 증가", year=2026, part="Q2", pspan="2분기"), None),
])
def test_abstain_reasons(client, p, reason):
    as_of = "2026-08-01" if p["id"] == "f" else AS_OF
    r = run(client, p, as_of=as_of)
    if reason is None:  # positive comparison base: verifiable, not abstained
        assert r["status"] in ("supported", "contradicted")
    else:
        assert r["status"] == "abstain" and any(reason in x for x in r["reasons"]), r["reasons"]


def test_growth_undefined_on_loss_base(client):
    # 2025 Q2 OFS owners' figure does not exist -> account missing -> abstain with a clear reason
    p = plan("o", "삼성전자 2분기 별도 지배주주순이익은 1조원", metric="net_income_owners", value="1조원", pspan="2분기", basis="OFS")
    r = run(client, p)
    assert r["status"] == "abstain" and "계정을 찾지 못했습니다" in r["reasons"][0]


def test_read_period_span():
    assert read_period_span("2분기", "2026-07-31") == {"part": "Q2", "scope": "quarter"}
    assert read_period_span("상반기", None) == {"part": "H1", "scope": "cumulative"}
    assert read_period_span("1~3분기 누적", None) == {"part": "Q3", "scope": "cumulative"}
    assert read_period_span("지난해", "2026-03-10") == {"year": 2025, "part": "FY"}
    assert read_period_span("2025년 말", None) == {"year": 2025, "part": "FY", "scope": "point"}
    assert read_period_span("3분기 말", None)["scope"] == "point"
    assert read_period_span("전년 동기 대비", None) == {}


def test_markdown_report(client):
    rs = [run(client, plan("a", "삼성전자의 2분기 영업이익은 89조4,924억원이었다", value="89조4,924억원", pspan="2분기")),
          run(client, plan("b", "삼성전자의 2분기 영업이익은 146조7,252억원이었다", value="146조7,252억원", pspan="2분기"))]
    md = render_markdown(rs, "default")
    assert "✅ 지지" in md and "❌ 모순" in md and "상반기 누적 값" in md and "접수 20260814003699" in md


def test_mcp_server_round_trip():
    env = {**os.environ, "FINGROUND_CACHE_DIR": str(FIXTURES), "PYTHONIOENCODING": "utf-8"}
    env.pop("DART_API_KEY", None)
    claim = plan("c1", "삼성전자의 2분기 영업이익은 146조7,252억원이었다", value="146조7,252억원", pspan="2분기")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "verify_claims", "arguments": {"as_of": AS_OF, "claims": [claim]}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lookup_facts", "arguments": {"company": "005930", "year": 2023, "part": "FY"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "nope/method"},
    ]
    proc = subprocess.run([sys.executable, "-m", "finground_kr.mcp_server"], cwd=Path(__file__).resolve().parents[1],
                          input=("\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) + "\n").encode("utf-8"),
                          capture_output=True, env=env, timeout=60)
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.decode("utf-8").splitlines())}
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in replies[2]["result"]["tools"]] == ["verify_claims", "lookup_facts", "resolve_company", "list_metrics"]
    body = replies[3]["result"]
    text = body["content"][0]["text"]
    assert body["isError"] is False and "❌ 모순" in text and "상반기 누적" in text and "structuredContent" not in body
    assert "내장 목록" not in text  # fixtures carry a corp_codes cache, so the seed note must not appear
    assert replies[4]["result"]["isError"] is True and "DART_API_KEY" in replies[4]["result"]["content"][0]["text"]
    assert replies[5]["error"]["code"] == -32601
    assert proc.stderr == b""
