"""DART OpenAPI client with a disk cache and an offline mode.

The API key is read from the DART_API_KEY environment variable only. It is never logged, written to
disk or included in error messages. Get a free key at https://opendart.fss.or.kr.

Endpoints:
  corpCode.xml           zip of every company: corp_code, corp_name, stock_code (cached as JSON)
  company.json           company profile; `acc_mt` is the fiscal year-end month
  fnlttSinglAcntAll.json full financial statements of one periodic report

Report columns (confirmed against live responses, 2026-10):
  annual (FY, 11011):   thstrm_amount = year, frmtrm_amount = prior year, bfefrmtrm_amount = two years ago
  quarterly (Q1 11013, H1 11012, Q3 11014):
      income statement: thstrm_amount = the 3 months, thstrm_add_amount = cumulative from January,
                        frmtrm_q_amount / frmtrm_add_amount = the same columns one year earlier
      balance sheet:    thstrm_amount = period end, frmtrm_amount = PRIOR FISCAL YEAR END (not the
                        same quarter a year earlier)
  DART returns the latest (amended) version of a report; `filed` is taken from the receipt number.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE_URL = "https://opendart.fss.or.kr/api/"
KEY_ENV = "DART_API_KEY"
REPORT_CODES = {"Q1": "11013", "H1": "11012", "Q3": "11014", "FY": "11011"}
PERIOD_OF_CODE = {v: k for k, v in REPORT_CODES.items()}
PERIOD_LABEL = {"Q1": "1분기보고서", "H1": "반기보고서", "Q3": "3분기보고서", "FY": "사업보고서"}
BASIS_LABEL = {"CFS": "연결", "OFS": "별도"}
STATUS = {
    "010": "등록되지 않은 키", "011": "사용할 수 없는 키", "012": "접근할 수 없는 IP", "013": "조회된 데이터가 없음",
    "014": "파일이 존재하지 않음", "020": "요청 제한(일 10,000건) 초과", "021": "조회 가능한 회사 개수 초과",
    "100": "필드의 부적절한 값", "101": "부적절한 접근", "800": "DART 시스템 점검 중", "900": "정의되지 않은 오류",
    "901": "사용자 계정의 개인정보 보유기간 만료",
}


class DartError(RuntimeError):
    def __init__(self, message: str, status: str | None = None) -> None:
        super().__init__(message)
        self.status = status


def parse_amount(text) -> Decimal | None:
    s = str(text if text is not None else "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def filed_date(rcept_no: str) -> str | None:
    s = str(rcept_no or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else None


@dataclass(frozen=True)
class Row:
    sj_div: str            # BS | IS | CIS | CF | SCE
    account_id: str
    account_nm: str
    account_detail: str          # DART sub-label, e.g. "지배기업 소유주지분" under 분기순이익
    thstrm: Decimal | None       # current period (3 months in quarterly reports; the year in annual reports)
    thstrm_add: Decimal | None   # cumulative from the start of the fiscal year (quarterly reports)
    frmtrm: Decimal | None       # prior year (annual) / prior fiscal-year end (quarterly balance sheet)
    frmtrm_q: Decimal | None     # same 3 months one year earlier (quarterly income statement)
    frmtrm_add: Decimal | None   # same cumulative period one year earlier
    bfefrmtrm: Decimal | None    # two years earlier (annual)
    thstrm_nm: str
    ord: int

    def column(self, name: str) -> Decimal | None:
        return getattr(self, name)

    @property
    def label(self) -> str:
        return f"{self.account_nm} ({self.account_detail})" if self.account_detail and self.account_detail != self.account_nm else self.account_nm


@dataclass
class Report:
    corp_code: str
    year: int
    period: str            # Q1 | H1 | Q3 | FY
    basis: str             # CFS | OFS
    rcept_no: str
    filed: str | None
    currency: str
    rows: list[Row] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.year}년 {PERIOD_LABEL[self.period]} ({BASIS_LABEL[self.basis]})"

    @property
    def url(self) -> str:
        return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={self.rcept_no}"

    def find(self, sj_divs: tuple[str, ...], account_ids: tuple[str, ...], names: tuple[str, ...]) -> Row | None:
        """First row matching an account id (preferred) or a normalized Korean account name."""
        for sj in sj_divs:
            cands = [r for r in self.rows if r.sj_div == sj]
            for r in cands:
                if r.account_id in account_ids:
                    return r
            for r in cands:
                if _norm_name(r.account_nm) in names:
                    return r
        return None


def _norm_name(name: str) -> str:
    s = re.sub(r"\s+", "", str(name))
    s = re.sub(r"\(.*?\)", "", s)   # 영업이익(손실) -> 영업이익
    return s


def normalize(raw: dict, corp_code: str, year: int, period: str, basis: str) -> Report:
    rows_raw = raw.get("list") or []
    rows = []
    for r in rows_raw:
        rows.append(Row(
            sj_div=r.get("sj_div", ""), account_id=r.get("account_id", ""), account_nm=str(r.get("account_nm", "")).strip(),
            account_detail=str(r.get("account_detail") or "").replace("-", "").strip(),
            thstrm=parse_amount(r.get("thstrm_amount")), thstrm_add=parse_amount(r.get("thstrm_add_amount")),
            frmtrm=parse_amount(r.get("frmtrm_amount")), frmtrm_q=parse_amount(r.get("frmtrm_q_amount")),
            frmtrm_add=parse_amount(r.get("frmtrm_add_amount")), bfefrmtrm=parse_amount(r.get("bfefrmtrm_amount")),
            thstrm_nm=str(r.get("thstrm_nm", "")), ord=int(r.get("ord") or 0),
        ))
    rcept = rows_raw[0].get("rcept_no", "") if rows_raw else ""
    currency = rows_raw[0].get("currency", "KRW") if rows_raw else "KRW"
    return Report(corp_code, year, period, basis, rcept, filed_date(rcept), currency, rows)


class DartClient:
    """Thin client. `cache_dir` stores raw JSON responses; `offline=True` never touches the network."""

    def __init__(self, cache_dir: str | Path, api_key: str | None = None, offline: bool = False,
                 pause: float = 0.25, timeout: float = 60.0) -> None:
        self.cache_dir = Path(cache_dir)
        self.offline = offline
        self.pause = pause
        self.timeout = timeout
        self._key = (api_key if api_key is not None else os.environ.get(KEY_ENV, "")).strip()
        if self._key.startswith("${"):  # unsubstituted plugin variable
            self._key = ""
        self._corp_codes: list[dict] | None = None

    # ------------------------------------------------------------------ transport
    def _require_key(self) -> None:
        if self.offline:
            raise DartError("offline mode: the requested data is not cached")
        if not self._key:
            raise DartError(f"{KEY_ENV} is not set. Get a free key at https://opendart.fss.or.kr and export it yourself; "
                            "this tool never stores it.")

    def _get(self, endpoint: str, **params) -> bytes:
        self._require_key()
        query = urllib.parse.urlencode({"crtfc_key": self._key, **params})
        try:
            with urllib.request.urlopen(BASE_URL + endpoint + "?" + query, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise DartError(f"DART {endpoint} returned HTTP {exc.code}") from None  # no URL: it carries the key
        except urllib.error.URLError as exc:
            raise DartError(f"cannot reach DART ({exc.reason})") from None
        time.sleep(self.pause)
        return body

    def _json(self, endpoint: str, **params) -> dict:
        data = json.loads(self._get(endpoint, **params).decode("utf-8"))
        status = str(data.get("status", ""))
        if status not in ("000", "013"):
            raise DartError(f"DART {endpoint} status {status}: {STATUS.get(status, data.get('message', ''))}", status)
        return data

    def _cached(self, name: str, fetch) -> dict:
        path = self.cache_dir / f"{name}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        data = fetch()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    # ------------------------------------------------------------------ companies
    def corp_codes(self) -> list[dict]:
        if self._corp_codes is None:
            def fetch():
                body = self._get("corpCode.xml")
                if not body.startswith(b"PK"):
                    m = re.search(rb"<status>(\d+)</status>", body)
                    status = m.group(1).decode() if m else "?"
                    raise DartError(f"DART corpCode.xml status {status}: {STATUS.get(status, body[:120].decode('utf-8', 'replace'))}", status)
                with zipfile.ZipFile(io.BytesIO(body)) as zf:
                    root = ET.fromstring(zf.read(zf.namelist()[0]))
                return {"list": [{k: (el.findtext(k) or "").strip() for k in ("corp_code", "corp_name", "stock_code", "modify_date")}
                                 for el in root.iter("list")]}
            self._corp_codes = self._cached("corp_codes", fetch)["list"]
        return self._corp_codes

    def resolve(self, query: str) -> list[dict]:
        """Listed companies matching a stock code, corp code or (space-insensitive) name. Exact matches only."""
        q = re.sub(r"\s+", "", str(query)).lower()
        listed = [r for r in self.corp_codes() if r["stock_code"]]
        hits = [r for r in listed if q in (r["stock_code"], r["corp_code"]) or re.sub(r"\s+", "", r["corp_name"]).lower() == q]
        if not hits:
            hits = [r for r in listed if re.sub(r"\s+", "", r["corp_name"]).lower() == q.replace("(주)", "").replace("주식회사", "")]
        return hits

    def company(self, corp_code: str) -> dict:
        return self._cached(f"company_{corp_code}", lambda: self._json("company.json", corp_code=corp_code))

    # ------------------------------------------------------------------ statements
    def report(self, corp_code: str, year: int, period: str, basis: str) -> Report | None:
        """None when DART has no such report (status 013)."""
        code = REPORT_CODES[period]
        raw = self._cached(f"fs_{corp_code}_{year}_{code}_{basis}",
                           lambda: self._json("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=str(year),
                                              reprt_code=code, fs_div=basis))
        if str(raw.get("status")) == "013" or not raw.get("list"):
            return None
        return normalize(raw, corp_code, year, period, basis)
