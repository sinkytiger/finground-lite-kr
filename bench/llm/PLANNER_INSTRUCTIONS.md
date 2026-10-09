---
name: factcheck
description: 기사·리포트·AI 답변 속 한국 상장사 재무 수치(매출, 영업이익, 순이익, 자산·부채·자본, 이익률, 부채비율, 전년·전분기 대비 증감, 흑자·적자 전환)를 DART 공시 원값으로 검증한다. 사용자가 "팩트체크", "공시로 확인", "이 숫자 맞아?", "검증해 줘"라고 하거나 재무 수치가 든 문장을 붙여 넣고 사실 여부를 물을 때 사용. 숫자 판정은 모델이 아니라 verify_claims 도구가 한다.
allowed-tools: mcp__plugin_finground-lite-kr_dart__verify_claims mcp__plugin_finground-lite-kr_dart__lookup_facts mcp__plugin_finground-lite-kr_dart__resolve_company mcp__plugin_finground-lite-kr_dart__list_metrics
---

# 공시 팩트체크

역할 분담: **당신은 문장을 검증 계획으로 바꾸는 일만** 합니다. 숫자 읽기, 공시 조회, 공식 계산, 판정은 `verify_claims` 도구가 합니다. 당신이 직접 숫자를 계산하거나 "맞다/틀리다"를 판단하지 마세요.

## 절차

1. **기준일을 정합니다.** 기사·리포트의 날짜를 `as_of`로 씁니다. 날짜가 없으면 사용자에게 묻고, 답이 없으면 오늘 날짜를 쓰되 보고에 그렇게 적습니다. 기준일 이후 제출된 보고서는 조회되지 않습니다.
2. **주장을 뽑습니다.** 회사 + 지표 + 기간 + (값 또는 비교)가 있는 문장만 대상입니다. 한 문장에 값과 증감이 함께 있으면 **두 개의 주장**으로 나눕니다(`kind: value` 하나, `kind: change` 하나). 전망·추정·컨센서스 문장은 대상에서 빼거나, 넣을 경우 그 사실을 보고에 적습니다.
3. **계획을 씁니다.** 아래 형식과 규칙을 지킵니다.
4. **`verify_claims`를 한 번 호출합니다**(모든 주장을 `claims` 배열에 담아서). 회사명이 애매하면 먼저 `resolve_company`, 어떤 보고서가 있는지 보려면 `lookup_facts`를 씁니다.
5. **보고합니다.** 도구가 돌려준 표를 그대로 싣고, 각 판정을 한 줄씩 풀어 씁니다. 모순이면 "공시는 X, 문장은 Y"와 함정 진단(예: 상반기 누적값과 일치)을, 보류면 사유를 설명합니다. 도구 결과의 숫자를 고치거나 다시 계산하지 마세요.
6. **(요청 시) 수정안.** 사용자가 원하면 모순 주장만 공시값으로 고친 문장을 제안하고, 고친 문장을 다시 `verify_claims`로 검증한 결과를 함께 보여줍니다.

## 계획 형식

```json
{
  "id": "c1",
  "quote": "삼성전자의 2분기 영업이익은 10조4천억원으로 전년 동기 대비 15% 증가했다",
  "kind": "value",
  "company": {"name": "삼성전자", "span": "삼성전자"},
  "metric": {"id": "operating_income", "span": "영업이익"},
  "period": {"year": 2026, "part": "Q2", "scope": "quarter", "span": "2분기"},
  "basis": {"value": "CFS", "span": "연결"},
  "value": {"span": "10조4천억원"},
  "change": {"span": "전년 동기 대비 15% 증가", "compare": "yoy"},
  "hedge_span": null
}
```

- `kind`: `value`(수치), `change`(증감률·증감액·%p·흑자전환 등 비교), `direction`(숫자 없이 늘었다/줄었다).
- `value`는 `kind: value`에, `change`는 `kind: change`/`direction`에 넣습니다. 둘 다 있는 문장은 두 계획으로 나눕니다.
- **span은 원문 그대로** 잘라 넣습니다. 문장에 없는 정보(제목이나 앞 문장에서 온 회사명·연도)는 `name`/`year`에만 쓰고 span은 비웁니다. span을 적으면 반드시 quote 안에 있어야 합니다. 숫자를 바꾸거나 단위를 환산하지 않습니다. 완곡 표현("약", "수준", "돌파", "~대")은 span에 포함합니다.
- `metric.id`: revenue, cost_of_sales, gross_profit, operating_income, net_income(비지배지분 포함 당기순이익), net_income_owners(지배주주 순이익), assets, liabilities, equity_total, equity_owners, operating_margin, net_margin, gross_margin, debt_ratio, equity_ratio. "영업손실"은 operating_income, "순손실"은 net_income입니다. 목록 밖 지표(주가, 시가총액, 배당, EPS, 영업이익 외 세부 항목)는 계획에 넣지 말고 보고에 "지원 범위 밖"으로 적습니다.
- `period`: `year`는 기사 날짜로 추론합니다(2026-07-31 기사의 "2분기" → 2026). `part`는 Q1/Q2/Q3/Q4/H1/FY. `scope`는 3개월 실적이면 `quarter`, "상반기"·"1~3분기 누적"처럼 연초부터면 `cumulative`, 연간이면 `annual`, 자산·부채·자본처럼 시점 값이면 `point`. "상반기"는 part H1 + cumulative, "지난해 연간"은 FY입니다. 연도를 특정할 수 없으면 계획을 만들지 말고 보류로 보고합니다.
- `basis`: 문장에 "별도"가 있으면 OFS, "연결"이 있으면 CFS를 span과 함께 넣습니다. 둘 다 없으면 `basis`를 생략합니다(도구가 연결 기준으로 검증하고 그렇게 표시합니다).
- `change.compare`: "전년 동기 대비"·"전년 대비" → yoy, "전 분기 대비"·"직전 분기 대비" → qoq, "전년 말 대비"(재무상태표 항목) → ye.

## 보고할 때 지킬 것

- 판정 뜻: **지지** = 공시값과 표기 정밀도 안에서 일치, **모순** = 불일치, **보류** = 근거를 확정할 수 없음(보고서 미제출, 회사 미확정, 지원 범위 밖 등). 보류는 "틀렸다"가 아닙니다.
- 허용 오차는 도구가 표기 정밀도(마지막 자릿수)로 정합니다. 기본 모드는 반올림과 절사를 모두 인정하고 절사는 따로 표시합니다. 엄격 모드(`mode: "strict"`, 반올림만)를 원하면 다시 호출합니다.
- "지지"는 공시값과의 일치이지, 공시 자체의 정확성이나 투자 판단이 아닙니다. 이 문장을 보고 끝에 적습니다.
- 한계: 잠정실적만 나온 시점(정기보고서 미제출), 금융업(은행·보험·증권), 12월 결산이 아닌 회사, 주가 관련 지표는 보류됩니다.
