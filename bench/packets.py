"""Build and score the LLM-in-the-loop experiments.

E2  planner:   sentence (+ as_of) -> the model writes verification plans; the verifier judges them.
E3  judge:     sentence + the filed figures (lookup_facts table) -> the model says accept / reject.
E3' judge+formula: additionally the authorized formula and the bound operand values (VeriFin's Judge+Formula).

    python -m bench.packets build  bench/data/samsung_fixture.jsonl --cache tests/fixtures/dart --offline --n 60 --seed 7 --out bench/llm/
    python -m bench.packets score  bench/llm/ --answers bench/llm/answers/ --cache tests/fixtures/dart --offline --out reports/llm.md

Model answers are collected outside this script (in Claude Code, by a subagent or by hand) as JSON files
named after the packet: planner_<id>.json = {"plans": [...]}, judge_<id>.json = {"verdict": "accept"|"reject"|"abstain"}.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from finground_kr.dart import DartClient
from finground_kr.facts import DERIVED_METRICS, FactStore, Period, Unavailable, metric_label
from finground_kr.mcp_server import Tools
from finground_kr.numbers import consistent, fmt_krw, fmt_rate
from finground_kr.verify import Verifier
from finground_kr import numbers as N

PLANNER_INSTRUCTIONS = (Path(__file__).resolve().parents[1] / "skills" / "factcheck" / "SKILL.md").read_text(encoding="utf-8")

JUDGE_PROMPT = """당신은 금융 기사 팩트체커입니다. 아래 문장이 공시 수치와 맞는지 판정하세요.
기준일: {as_of}
문장: {sentence}

공시 수치 (DART, 금액 단위 원):
{facts}

{formula}규칙: 표기 정밀도(마지막 자릿수)의 절반까지 반올림 오차를 허용합니다. 근거가 부족하면 abstain.
JSON 한 줄로만 답하세요: {{"verdict": "accept" | "reject" | "abstain", "reason": "..."}}"""


def _facts_table(tools: Tools, sample: dict, plan: dict) -> str:
    year, part = plan["period"]["year"], plan["period"]["part"]
    rp = {"Q1": "Q1", "Q2": "H1", "H1": "H1", "Q3": "Q3", "Q4": "FY", "FY": "FY"}[part]
    parts = []
    for y, p in ((year, rp), (year, "Q3") if part == "Q4" else (None, None), (year - 1, rp) if part in ("Q4", "FY") else (None, None)):
        if y is None:
            continue
        try:
            parts.append(tools.lookup_facts(sample["company"]["corp_code"], y, p, plan.get("basis", {}).get("value", "CFS"), sample["as_of"])["markdown"])
        except Exception as exc:  # noqa: BLE001 - report gaps as part of the packet
            parts.append(f"({y} {p}: {exc})")
    return "\n\n".join(parts)


def _formula_block(store: FactStore, sample: dict, plan: dict) -> str:
    mid, basis = plan["metric"]["id"], plan.get("basis", {}).get("value", "CFS")
    period = Period(plan["period"]["year"], plan["period"]["part"], plan["period"].get("scope") or "quarter")
    corp = sample["company"]["corp_code"]
    try:
        if plan["kind"] == "value":
            if mid in DERIVED_METRICS:
                f = store.derived(corp, mid, period, basis)
                return f"공식: {DERIVED_METRICS[mid]['formula']}; 피연산자: " + ", ".join(f"{metric_label(k)}={fmt_krw(v)}" for k, v in f.components.items()) + "\n\n"
            f = store.base(corp, mid, period, basis)
            return f"해당 값: {metric_label(mid)} {period.label()} = {fmt_krw(f.value)} ({f.derivation or '공시값'})\n\n"
        cur = store.base(corp, mid, period, basis) if mid not in DERIVED_METRICS else store.derived(corp, mid, period, basis)
        compare = plan["change"].get("compare", "yoy")
        prev = (store.prior_year if compare == "yoy" else store.prior_quarter)(corp, mid, period, basis) if mid not in DERIVED_METRICS else store.derived(corp, mid, period, basis, compare)
        fmt = fmt_krw if cur.unit == "KRW" else (lambda x: fmt_rate(x, cur.unit))
        return (f"공식: 증감률 = (당기 − 전기) ÷ 전기 × 100, 증감액 = 당기 − 전기, %p = 당기 비율 − 전기 비율\n"
                f"피연산자: 당기 {period.label()} = {fmt(cur.value)}, 전기 {prev.period.label()} = {fmt(prev.value)}\n\n")
    except Unavailable as exc:
        return f"(공식/피연산자 확정 불가: {exc})\n\n"


def build(args) -> int:
    samples = [json.loads(l) for l in open(args.data, encoding="utf-8") if l.strip()]
    rng = random.Random(args.seed)
    singles = [s for s in samples if s["style"] != "combined"]
    correct = [s for s in singles if s["labels"]["default"] == "correct"]
    wrong = [s for s in singles if s["labels"]["default"] == "incorrect"]
    combined = [s for s in samples if s["style"] == "combined"]
    pick = rng.sample(correct, min(args.n // 2, len(correct))) + rng.sample(wrong, min(args.n - args.n // 2, len(wrong)))
    pick += rng.sample(combined, min(args.n_combined, len(combined)))
    client = DartClient(args.cache, offline=args.offline)
    tools, store = Tools(client), FactStore(client)
    out = Path(args.out)
    for sub in ("planner", "judge", "judge_formula", "answers"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    manifest = []
    for s in pick:
        sid = s["id"]
        (out / "planner" / f"{sid}.json").write_text(json.dumps({"id": sid, "as_of": s["as_of"], "sentence": s["sentence"],
                                                                 "company_hint": s["company"]["name"]}, ensure_ascii=False, indent=1), encoding="utf-8")
        if s["style"] != "combined":
            plan = s["plans"][0]
            facts = _facts_table(tools, s, plan)
            (out / "judge" / f"{sid}.md").write_text(JUDGE_PROMPT.format(as_of=s["as_of"], sentence=s["sentence"], facts=facts, formula=""), encoding="utf-8")
            (out / "judge_formula" / f"{sid}.md").write_text(JUDGE_PROMPT.format(as_of=s["as_of"], sentence=s["sentence"], facts=facts,
                                                                                formula=_formula_block(store, s, plan)), encoding="utf-8")
        manifest.append({"id": sid, "label": s["labels"]["default"], "label_strict": s["labels"]["strict"], "trap": (s.get("trap") or {}).get("type"),
                         "style": s["style"], "n_plans": len(s["plans"])})
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "PLANNER_INSTRUCTIONS.md").write_text(PLANNER_INSTRUCTIONS, encoding="utf-8")
    print(f"built {len(pick)} packets in {out} ({len([m for m in manifest if m['label'] == 'correct'])} correct, "
          f"{len([m for m in manifest if m['label'] == 'incorrect'])} incorrect, {len(combined and [m for m in manifest if m['style'] == 'combined'])} combined)")
    return 0


def _outcome(label: str, decision: str) -> str:
    if decision == "abstain":
        return "Abs"
    if label == "correct":
        return "TA" if decision == "accept" else "FR"
    return "FA" if decision == "accept" else "TR"


def score(args) -> int:
    out = Path(args.out_dir)
    manifest = {m["id"]: m for m in json.loads((out / "manifest.json").read_text(encoding="utf-8"))}
    samples = {json.loads(l)["id"]: json.loads(l) for l in open(args.data, encoding="utf-8") if l.strip()}
    answers = Path(args.answers)
    client = DartClient(args.cache, offline=args.offline)
    verifier = Verifier(client, "default")
    results: dict[str, Counter] = {"planner": Counter(), "judge": Counter(), "judge_formula": Counter()}
    details = []
    for sid, m in manifest.items():
        s = samples[sid]
        gold_plans = s["plans"]
        # E2: model-written plans -> verifier
        pf = answers / f"planner_{sid}.json"
        if pf.exists():
            plans = json.loads(pf.read_text(encoding="utf-8")).get("plans", [])
            if not plans:
                results["planner"]["Abs"] += 1
            else:
                statuses = [verifier.verify(p, s["as_of"]).as_dict()["status"] for p in plans]
                # the sentence is accepted only if every plan is supported; any contradiction rejects; otherwise abstain
                decision = "reject" if "contradicted" in statuses else "accept" if all(x == "supported" for x in statuses) else "abstain"
                label = m["label"] if m["style"] != "combined" else "correct"
                results["planner"][_outcome(label, decision)] += 1
                results["planner"]["plans_written"] += len(plans)
                results["planner"]["plans_expected"] += len(gold_plans)
                details.append({"id": sid, "exp": "planner", "label": label, "decision": decision, "statuses": statuses})
        for exp in ("judge", "judge_formula"):
            jf = answers / f"{exp}_{sid}.json"
            if jf.exists() and m["style"] != "combined":
                verdict = json.loads(jf.read_text(encoding="utf-8")).get("verdict", "abstain")
                results[exp][_outcome(m["label"], verdict)] += 1
                details.append({"id": sid, "exp": exp, "label": m["label"], "decision": verdict, "trap": m["trap"]})
    md = ["# LLM 실험 결과 (E2 계획 작성, E3 LLM 판정자)", "", "| 실험 | n | TA | FA | TR | FR | 보류 | 수용 정밀도 | 정확도 | 커버리지 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for exp, c in results.items():
        n = sum(c[k] for k in ("TA", "FA", "TR", "FR", "Abs"))
        if not n:
            continue
        dec = n - c["Abs"]
        prec = c["TA"] / (c["TA"] + c["FA"]) if c["TA"] + c["FA"] else None
        acc = (c["TA"] + c["TR"]) / dec if dec else None
        pct = lambda x: "-" if x is None else f"{x * 100:.1f}%"
        md.append(f"| {exp} | {n} | {c['TA']} | {c['FA']} | {c['TR']} | {c['FR']} | {c['Abs']} | {pct(prec)} | {pct(acc)} | {pct(dec / n)} |")
    fa = [d for d in details if d["exp"] != "planner" and _outcome(d["label"], d["decision"]) == "FA"]
    if fa:
        md += ["", "## LLM 판정자가 수용한 틀린 주장", ""] + [f"- {d['exp']} {d['id']} (함정 {d['trap']})" for d in fa]
    text = "\n".join(md)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        Path(args.out).with_suffix(".json").write_text(json.dumps({"results": {k: dict(v) for k, v in results.items()}, "details": details}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("data"); b.add_argument("--cache", required=True); b.add_argument("--offline", action="store_true")
    b.add_argument("--n", type=int, default=60); b.add_argument("--n-combined", type=int, default=10); b.add_argument("--seed", type=int, default=7)
    b.add_argument("--out", required=True)
    sc = sub.add_parser("score")
    sc.add_argument("out_dir"); sc.add_argument("--data", required=True); sc.add_argument("--answers", required=True)
    sc.add_argument("--cache", required=True); sc.add_argument("--offline", action="store_true"); sc.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    return build(args) if args.cmd == "build" else score(args)


if __name__ == "__main__":
    raise SystemExit(main())
