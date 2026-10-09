"""E1: run the verifier on gold plans and score it as an acceptance control (VeriFin metrics).

    TA/FA/TR/FR/Abs, accepted-claim precision TA/(TA+FA), accuracy (TA+TR)/decided, coverage 1 - Abs/n,
    plus diagnosis hit rate on trap samples and spurious-diagnosis rate on near misses.

    python -m bench.evaluate bench/data/samsung.jsonl --cache tests/fixtures/dart --offline --out reports/e1.md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from finground_kr.dart import DartClient
from finground_kr.verify import Verifier


def score(samples: list[dict], client: DartClient, mode: str) -> dict:
    verifier = Verifier(client, mode)
    counts = Counter()
    by_trap: dict[str, Counter] = defaultdict(Counter)
    by_style: dict[str, Counter] = defaultdict(Counter)
    rows = []
    for s in samples:
        if s["style"] == "combined":
            continue  # planner experiment only
        label = s["labels"][mode]
        plan = s["plans"][0]
        v = verifier.verify(plan, s["as_of"]).as_dict()
        st = v["status"]
        outcome = ("Abs" if st == "abstain" else
                   "TA" if label == "correct" and st == "supported" else
                   "FA" if label == "incorrect" and st == "supported" else
                   "TR" if label == "incorrect" and st == "contradicted" else "FR")
        counts[outcome] += 1
        trap = (s.get("trap") or {}).get("type") or ("near_miss" if "near_miss" in s["id"] else "correct")
        by_trap[trap][outcome] += 1
        by_style[s["style"]][outcome] += 1
        diag_hit = None
        exp = (s.get("trap") or {}).get("expected_diagnosis")
        if st == "contradicted":
            if exp:
                diag_hit = any(exp in d["interpretation"] for d in v["diagnosis"])
                by_trap[trap]["diag_hit" if diag_hit else "diag_miss"] += 1
            elif "near_miss" in s["id"]:
                by_trap[trap]["spurious_diag" if v["diagnosis"] else "no_diag"] += 1
        rows.append({"id": s["id"], "label": label, "status": st, "outcome": outcome, "trap": trap, "style": s["style"],
                     "diag_hit": diag_hit, "reasons": v["reasons"], "diagnosis": [d["interpretation"] for d in v["diagnosis"]]})
    n = sum(counts.values())
    decided = n - counts["Abs"]
    summary = {"mode": mode, "n": n, **{k: counts[k] for k in ("TA", "FA", "TR", "FR", "Abs")},
               "precision": counts["TA"] / (counts["TA"] + counts["FA"]) if counts["TA"] + counts["FA"] else None,
               "accuracy": (counts["TA"] + counts["TR"]) / decided if decided else None,
               "coverage": decided / n if n else None}
    return {"summary": summary, "by_trap": {k: dict(v) for k, v in by_trap.items()}, "by_style": {k: dict(v) for k, v in by_style.items()}, "rows": rows}


def render(results: dict[str, dict], source: str) -> str:
    md = [f"# E1 검증기 평가 (정답 계획 입력)", "", f"데이터: `{source}`", "",
          "| 모드 | n | TA | FA | TR | FR | 보류 | 수용 정밀도 | 정확도 | 커버리지 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for mode, r in results.items():
        s = r["summary"]
        pct = lambda x: "-" if x is None else f"{x * 100:.1f}%"
        md.append(f"| {mode} | {s['n']} | {s['TA']} | {s['FA']} | {s['TR']} | {s['FR']} | {s['Abs']} | {pct(s['precision'])} | {pct(s['accuracy'])} | {pct(s['coverage'])} |")
    md += ["", "TA/FA = 맞는/틀린 주장을 지지, TR/FR = 틀린/맞는 주장을 모순으로 판정. 라벨은 허용 오차 규칙으로 정의되며 모드마다 다릅니다.", ""]
    for mode, r in results.items():
        md += [f"## {mode} 모드: 함정 유형별", "", "| 유형 | n | TA | FA | TR | FR | 보류 | 진단 적중 | 진단 누락 | 근접 오류의 허위 진단 |", "|---|---|---|---|---|---|---|---|---|---|"]
        for trap, c in sorted(r["by_trap"].items()):
            n = sum(c.get(k, 0) for k in ("TA", "FA", "TR", "FR", "Abs"))
            md.append(f"| {trap} | {n} | {c.get('TA', 0)} | {c.get('FA', 0)} | {c.get('TR', 0)} | {c.get('FR', 0)} | {c.get('Abs', 0)} | "
                      f"{c.get('diag_hit', '-')} | {c.get('diag_miss', '-')} | {c.get('spurious_diag', '-')} |")
        md += ["", f"## {mode} 모드: 표기 스타일별 (맞는 주장의 거짓 기각 확인)", "", "| 스타일 | TA | FR | 보류 | FA | TR |", "|---|---|---|---|---|---|"]
        for style, c in sorted(r["by_style"].items()):
            md.append(f"| {style} | {c.get('TA', 0)} | {c.get('FR', 0)} | {c.get('Abs', 0)} | {c.get('FA', 0)} | {c.get('TR', 0)} |")
        md.append("")
        fails = [row for row in r["rows"] if row["outcome"] in ("FA", "FR")]
        if fails:
            md += [f"### {mode} 모드 오판 목록", ""] + [f"- {row['id']}: {row['outcome']} ({row['status']}, 진단 {row['diagnosis'] or '-'}, {row['reasons'] or ''})" for row in fails[:40]] + [""]
        abst = [row for row in r["rows"] if row["outcome"] == "Abs"]
        if abst:
            md += [f"### {mode} 모드 보류 사유", ""] + [f"- {k}: {v}" for k, v in Counter(x for row in abst for x in row["reasons"]).most_common(10)] + [""]
    return "\n".join(md)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    samples = [json.loads(l) for l in open(args.data, encoding="utf-8") if l.strip()]
    client = DartClient(args.cache, offline=args.offline)
    results = {mode: score(samples, client, mode) for mode in ("default", "strict")}
    md = render(results, args.data)
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        Path(args.out).with_suffix(".json").write_text(json.dumps({m: {"summary": r["summary"], "by_trap": r["by_trap"], "rows": r["rows"]} for m, r in results.items()},
                                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
