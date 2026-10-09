"""MCP server (stdio, newline-delimited JSON-RPC 2.0, standard library only).

Tools
  verify_claims    verify a list of claim plans against DART filings
  lookup_facts     key figures of one report (3-month, cumulative and prior-year columns)
  resolve_company  listed companies matching a name or code
  list_metrics     supported metrics and formulas

Run:  python -m finground_kr.mcp_server
Env:  DART_API_KEY (required for live lookups), FINGROUND_CACHE_DIR (optional), CLAUDE_PLUGIN_DATA (plugin cache root)
All logging goes to stderr; stdout carries protocol messages only.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

from . import __version__
from .dart import PERIOD_LABEL, BASIS_LABEL, DartClient, DartError
from .facts import BASE_METRICS, DERIVED_METRICS, FactStore, Period, Unavailable, metric_label
from .numbers import fmt_krw, fmt_rate
from .verify import Verifier

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "finground-kr", "version": __version__}
INSTRUCTIONS = ("Verifies Korean financial claims (매출·영업이익·순이익·자산·부채·자본, 이익률, 전년 대비 증감) against DART "
                "filings. Call verify_claims with claim plans whose spans quote the sentence verbatim; the server re-reads "
                "numbers itself and abstains when facts or formulas cannot be established.")

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "quote": {"type": "string", "description": "the sentence (or clause) verbatim"},
        "kind": {"type": "string", "enum": ["value", "change", "direction"]},
        "company": {"type": "object", "properties": {"name": {"type": "string"}, "span": {"type": "string"}}, "required": ["name"]},
        "metric": {"type": "object", "properties": {"id": {"type": "string"}, "span": {"type": "string"}}, "required": ["id"]},
        "period": {"type": "object", "properties": {"year": {"type": "integer"}, "part": {"type": "string", "enum": ["Q1", "Q2", "Q3", "Q4", "H1", "FY"]},
                                                    "scope": {"type": "string", "enum": ["quarter", "cumulative", "annual", "point"]},
                                                    "span": {"type": "string"}}, "required": ["year", "part"]},
        "basis": {"type": "object", "properties": {"value": {"type": "string", "enum": ["CFS", "OFS"]}, "span": {"type": "string"}}},
        "value": {"type": "object", "properties": {"span": {"type": "string"}}},
        "change": {"type": "object", "properties": {"span": {"type": "string"}, "compare": {"type": "string", "enum": ["yoy", "qoq", "ye"]}}},
        "hedge_span": {"type": "string"},
    },
    "required": ["id", "quote", "kind", "company", "metric", "period"],
}

TOOLS = [
    {
        "name": "verify_claims",
        "description": ("Verify numeric claims about Korean listed companies against DART periodic reports. Each claim is a plan: "
                        "company, metric id (revenue, operating_income, net_income, net_income_owners, assets, liabilities, equity_total, "
                        "equity_owners, cost_of_sales, gross_profit, operating_margin, net_margin, gross_margin, debt_ratio, equity_ratio), "
                        "period (year, part Q1/Q2/Q3/Q4/H1/FY, scope quarter/cumulative/annual/point), basis CFS/OFS, and the verbatim "
                        "text spans holding the number (value.span) or the comparison (change.span, compare yoy/qoq/ye). Do NOT convert "
                        "numbers yourself: pass spans. Returns supported / contradicted / abstain per claim with the filed value, tolerance, "
                        "trap diagnosis and citations (rcept_no, statement, account, column)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "claims": {"type": "array", "items": PLAN_SCHEMA},
                "as_of": {"type": "string", "description": "YYYY-MM-DD; reports filed after this date are ignored (use the article date)"},
                "mode": {"type": "string", "enum": ["default", "strict"], "description": "default = rounding or truncation; strict = rounding only"},
            },
            "required": ["claims"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "lookup_facts",
        "description": ("Key figures of one DART periodic report: revenue, operating income, net income (total and owners), assets, "
                        "liabilities, equity, with the 3-month, cumulative and prior-year columns, filed date and receipt number. "
                        "part: Q1, H1 (=Q2 report), Q3 or FY."),
        "inputSchema": {
            "type": "object",
            "properties": {"company": {"type": "string"}, "year": {"type": "integer"}, "part": {"type": "string", "enum": ["Q1", "H1", "Q3", "FY"]},
                           "basis": {"type": "string", "enum": ["CFS", "OFS"]}, "as_of": {"type": "string"}},
            "required": ["company", "year", "part"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "resolve_company",
        "description": "Listed companies whose name (space-insensitive) or 6-digit stock code matches the query exactly.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "list_metrics",
        "description": "Supported metric ids with Korean labels, DART account mapping and ratio formulas.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
]


def cache_dir() -> Path:
    for env in ("FINGROUND_CACHE_DIR", "CLAUDE_PLUGIN_DATA"):
        if os.environ.get(env):
            return Path(os.environ[env]) / ("" if env == "FINGROUND_CACHE_DIR" else "dart_cache")
    return Path.home() / ".cache" / "finground-kr"


# ---------------------------------------------------------------------- tool implementations
class Tools:
    def __init__(self, client: DartClient) -> None:
        self.client = client

    def verify_claims(self, claims: list, as_of: str | None = None, mode: str = "default") -> dict:
        verifier = Verifier(self.client, mode=mode or "default")
        results = [verifier.verify(plan, as_of).as_dict() for plan in claims]
        return {"as_of": as_of, "mode": mode or "default", "results": results, "markdown": render_markdown(results, mode or "default")}

    def lookup_facts(self, company: str, year: int, part: str, basis: str = "CFS", as_of: str | None = None) -> dict:
        hits = self.client.resolve(company)
        if len(hits) != 1:
            raise ToolError(f"'{company}' matched {len(hits)} listed companies")
        corp = hits[0]
        store = FactStore(self.client, as_of)
        rep = store.report(corp["corp_code"], int(year), part, basis)
        rows = []
        for mid, spec in BASE_METRICS.items():
            row = rep.find(spec["sj"], spec["ids"], spec["names"])
            if row is None:
                continue
            rows.append({"metric": mid, "label": spec["label"], "account": row.label, "statement": row.sj_div,
                         "current": _s(row.thstrm), "cumulative": _s(row.thstrm_add), "prior_year_same": _s(row.frmtrm_q),
                         "prior_year_cumulative": _s(row.frmtrm_add), "prior": _s(row.frmtrm), "two_years_ago": _s(row.bfefrmtrm)})
        md = [f"**{corp['corp_name']} ({corp['stock_code']}) {rep.label}** 접수번호 {rep.rcept_no}, 제출 {rep.filed}, {rep.url}", "",
              "| 지표 | 계정 | 당기(3개월/연간) | 누적 | 전년 동기 | 전년 누적 | 전기(말) |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            md.append(f"| {r['label']} | {r['account']} | {_k(r['current'])} | {_k(r['cumulative'])} | {_k(r['prior_year_same'])} | "
                      f"{_k(r['prior_year_cumulative'])} | {_k(r['prior'])} |")
        md.append("")
        md.append("분기·반기보고서: 당기 = 3개월, 누적 = 1월부터. 재무상태표의 '전기'는 전년 말입니다. 사업보고서: 당기 = 연간, 전기 = 전년.")
        return {"company": corp, "report": {"label": rep.label, "rcept_no": rep.rcept_no, "filed": rep.filed, "url": rep.url},
                "rows": rows, "markdown": "\n".join(md)}

    def resolve_company(self, query: str) -> dict:
        hits = self.client.resolve(query)
        return {"query": query, "matches": hits,
                "markdown": "\n".join(f"- {h['corp_name']} (종목 {h['stock_code']}, corp {h['corp_code']})" for h in hits) or "(no match)"}

    def list_metrics(self) -> dict:
        base = [{"id": k, "label": v["label"], "statement": v["sj"], "account_ids": v["ids"], "kind": "flow" if v["flow"] else "point"}
                for k, v in BASE_METRICS.items()]
        derived = [{"id": k, "label": v["label"], "formula": v["formula"], "unit": v["unit"], "source": v["source"]} for k, v in DERIVED_METRICS.items()]
        md = ["| id | 지표 | 구분 |", "|---|---|---|"] + [f"| {b['id']} | {b['label']} | {'재무상태표(기말)' if b['kind'] == 'point' else '손익(3개월/누적/연간)'} |" for b in base]
        md += ["", "| id | 지표 | 공식 |", "|---|---|---|"] + [f"| {d['id']} | {d['label']} | {d['formula']} |" for d in derived]
        return {"base": base, "derived": derived, "markdown": "\n".join(md)}


class ToolError(Exception):
    pass


def _s(v):
    return None if v is None else str(v)


def _k(v):
    return "-" if v is None else fmt_krw(v)


STATUS_KO = {"supported": "✅ 지지", "contradicted": "❌ 모순", "abstain": "⚪ 보류"}


def render_markdown(results: list[dict], mode: str) -> str:
    lines = [f"| # | 판정 | 주장 | 공시 기준값 | 차이 | 진단 | 출처 |", "|---|---|---|---|---|---|---|"]
    for r in results:
        claim = r.get("claim", {}).get("display") or r.get("claim", {}).get("text") or "-"
        exp = r.get("expected", {})
        exp_disp = exp.get("display") or (f"{exp.get('current')} vs {exp.get('previous')}" if exp.get("current") else "-")
        diff = exp.get("difference_display", "-")
        diag = "; ".join(d["interpretation"] + f" ({d['display']})" for d in r.get("diagnosis", [])) or ("-" if r["status"] != "abstain" else "; ".join(r.get("reasons", [])))
        cites = "; ".join(f"{c['report']} {c['statement']} {c['account']} {c['column']} (접수 {c['rcept_no']})" for c in r.get("citations", [])[:3]) or "-"
        lines.append(f"| {r['id']} | {STATUS_KO[r['status']]} | {claim} | {exp_disp} | {diff} | {diag} | {cites} |")
    notes = [f"- {r['id']}: {n}" for r in results for n in r.get("notes", [])]
    strict = [r for r in results if r.get("status_strict") and r["status_strict"] != r["status"]]
    tail = [f"허용 오차 모드: {mode} (표기 정밀도의 절반, 반올림" + (" 또는 절사" if mode == "default" else "") + ")."]
    if strict:
        tail.append("엄격 모드(반올림만)에서는 판정이 달라지는 주장: " + ", ".join(r["id"] for r in strict))
    tail.append("'지지'는 공시값과의 일치이지 공시 자체의 정확성이나 투자 판단을 뜻하지 않습니다.")
    return "\n".join(lines + ([""] + notes if notes else []) + [""] + tail)


# ---------------------------------------------------------------------- JSON-RPC loop
def _send(msg: dict) -> None:
    sys.stdout.buffer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _log(text: str) -> None:
    print(f"[finground-kr] {text}", file=sys.stderr, flush=True)


def serve(client: DartClient | None = None) -> None:
    client = client or DartClient(cache_dir())
    tools = Tools(client)
    impl = {"verify_claims": tools.verify_claims, "lookup_facts": tools.lookup_facts,
            "resolve_company": tools.resolve_company, "list_metrics": tools.list_metrics}
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if "id" not in req:  # notification
            continue
        rid, method, params = req["id"], req.get("method", ""), req.get("params") or {}
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                if name not in impl:
                    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
                    continue
                try:
                    data = impl[name](**args)
                    text = data.pop("markdown", None) or json.dumps(data, ensure_ascii=False, indent=1)
                    result = {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": False}
                except (ToolError, DartError, Unavailable, TypeError, ValueError) as exc:
                    result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
            else:
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
                continue
            _send({"jsonrpc": "2.0", "id": rid, "result": result})
        except Exception:  # never let a bug kill the server; never leak to stdout
            _log(traceback.format_exc())
            _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error"}})


if __name__ == "__main__":
    serve()
