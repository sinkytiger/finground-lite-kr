import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures" / "dart"

from finground_kr.dart import DartClient  # noqa: E402


@pytest.fixture(scope="session")
def client() -> DartClient:
    """Offline client over recorded (trimmed) DART responses for 삼성전자 2024 FY – 2026 H1."""
    return DartClient(FIXTURES, offline=True)


def plan(id, quote, kind="value", metric="operating_income", year=2026, part="Q2", scope=None, value=None, change=None,
         compare=None, basis=None, pspan=None, mspan=None, cname="삼성전자", cspan="삼성전자"):
    p = {"id": id, "quote": quote, "kind": kind, "company": {"name": cname, "span": cspan},
         "metric": {"id": metric, "span": mspan}, "period": {"year": year, "part": part, "scope": scope, "span": pspan}}
    if value:
        p["value"] = {"span": value}
    if change:
        p["change"] = {"span": change, "compare": compare or "yoy"}
    if basis:
        p["basis"] = {"value": basis, "span": "별도" if basis == "OFS" else "연결"}
    return p
