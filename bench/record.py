"""Record trimmed DART responses for a set of companies so benchmarks and tests run offline.

    python -m bench.record --cache <live cache dir> --out bench/fixtures/dart --companies 00126380 00164742 ...
                           [--years 2024 2025 2026]

Keeps only BS/IS/CIS rows for the accounts the verifier uses (same filter as tests/fixtures/dart) and writes
company profiles plus a corp_codes.json built from those profiles (stock_name as the display name).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from finground_kr.dart import REPORT_CODES, DartClient, DartError
from finground_kr.facts import BASE_METRICS

KEEP_IDS = {i for m in BASE_METRICS.values() for i in m["ids"]}
KEEP_NAMES = {n for m in BASE_METRICS.values() for n in m["names"]}
PERIODS = {2024: ["FY"], 2025: ["Q1", "H1", "Q3", "FY"], 2026: ["Q1", "H1"]}


def norm(s: str) -> str:
    return re.sub(r"\(.*?\)", "", re.sub(r"\s+", "", s))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", required=True, help="live cache directory (full responses)")
    ap.add_argument("--out", required=True, help="fixture directory (trimmed responses)")
    ap.add_argument("--companies", nargs="+", required=True, help="corp codes")
    ap.add_argument("--years", nargs="+", type=int, default=sorted(PERIODS))
    args = ap.parse_args(argv)
    live = DartClient(args.cache)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    codes = []
    for corp in args.companies:
        info = live.company(corp)
        if not info.get("stock_code"):
            print(f"skip {corp}: not listed")
            continue
        name = (info.get("stock_name") or info["corp_name"]).replace("(주)", "").replace("주식회사", "").strip()
        codes.append({"corp_code": corp, "corp_name": name, "stock_code": info["stock_code"], "modify_date": ""})
        (out / f"company_{corp}.json").write_text(json.dumps({k: info.get(k) for k in ("status", "corp_code", "corp_name", "stock_name", "stock_code", "corp_cls", "acc_mt")}, ensure_ascii=False), encoding="utf-8")
        n = 0
        for year in args.years:
            for p in PERIODS.get(year, []):
                for basis in ("CFS", "OFS"):
                    try:
                        raw = live._cached(f"fs_{corp}_{year}_{REPORT_CODES[p]}_{basis}",
                                           lambda: live._json("fnlttSinglAcntAll.json", corp_code=corp, bsns_year=str(year), reprt_code=REPORT_CODES[p], fs_div=basis))
                    except DartError as exc:
                        print(f"  {name} {year} {p} {basis}: {exc}")
                        continue
                    rows = raw.get("list") or []
                    kept = [r for r in rows if r.get("sj_div") in ("BS", "IS", "CIS") and (r.get("account_id") in KEEP_IDS or norm(r.get("account_nm", "")) in KEEP_NAMES)]
                    (out / f"fs_{corp}_{year}_{REPORT_CODES[p]}_{basis}.json").write_text(json.dumps({"status": raw.get("status"), "message": raw.get("message"), "list": kept}, ensure_ascii=False), encoding="utf-8")
                    n += bool(kept)
        print(f"{name} ({info['stock_code']}): {n} reports with rows")
    existing = []
    cc = out / "corp_codes.json"
    if cc.exists():
        existing = [c for c in json.loads(cc.read_text(encoding="utf-8"))["list"] if c["corp_code"] not in {x["corp_code"] for x in codes}]
    cc.write_text(json.dumps({"list": existing + codes, "note": "built from company.json profiles (corpCode.xml was unavailable); names are stock_name"}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"corp_codes.json: {len(existing) + len(codes)} companies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
