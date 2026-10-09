"""Command line:  python -m finground_kr.cli <command>

  verify  PLANS.json [--as-of DATE] [--mode default|strict] [--json]   verify claim plans (a JSON list)
  lookup  COMPANY YEAR PART [--basis CFS|OFS] [--as-of DATE]            key figures of one report
  resolve QUERY                                                           listed companies matching a name/code
  metrics                                                                 supported metrics
  serve                                                                   run the MCP server on stdio
Cache: --cache DIR (default: $FINGROUND_CACHE_DIR, $CLAUDE_PLUGIN_DATA/dart_cache or ~/.cache/finground-kr)
"""

from __future__ import annotations

import argparse
import json
import sys

from .dart import DartClient, DartError
from .facts import Unavailable
from .mcp_server import Tools, ToolError, cache_dir, serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="finground-kr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--offline", action="store_true", help="never call DART (cache/fixtures only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("plans")
    v.add_argument("--as-of", default=None)
    v.add_argument("--mode", choices=["default", "strict"], default="default")
    v.add_argument("--json", action="store_true")
    lk = sub.add_parser("lookup")
    lk.add_argument("company"); lk.add_argument("year", type=int); lk.add_argument("part", choices=["Q1", "H1", "Q3", "FY"])
    lk.add_argument("--basis", choices=["CFS", "OFS"], default="CFS"); lk.add_argument("--as-of", default=None)
    r = sub.add_parser("resolve"); r.add_argument("query")
    sub.add_parser("metrics")
    sub.add_parser("serve")
    args = ap.parse_args(argv)

    client = DartClient(args.cache or cache_dir(), offline=args.offline)
    if args.cmd == "serve":
        serve(client)
        return 0
    tools = Tools(client)
    try:
        if args.cmd == "verify":
            with open(args.plans, encoding="utf-8") as f:
                plans = json.load(f)
            out = tools.verify_claims(plans if isinstance(plans, list) else plans["claims"], args.as_of, args.mode)
            print(json.dumps(out["results"], ensure_ascii=False, indent=1) if args.json else out["markdown"])
        elif args.cmd == "lookup":
            print(tools.lookup_facts(args.company, args.year, args.part, args.basis, args.as_of)["markdown"])
        elif args.cmd == "resolve":
            print(tools.resolve_company(args.query)["markdown"])
        elif args.cmd == "metrics":
            print(tools.list_metrics()["markdown"])
    except (ToolError, DartError, Unavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
