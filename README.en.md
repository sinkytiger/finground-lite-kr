# finground-lite-kr

[한국어](README.md) · English

A Claude Code plugin that **verifies numeric claims about Korean listed companies against DART filings**. Give it a sentence such as "Samsung's Q2 operating profit was 10.4 trillion won, up 15% year on year"; it splits the sentence into atomic claims, checks each number against the filed value, and when a number is wrong it tells you **which common misreading it matches** (separate instead of consolidated statements, cumulative instead of quarterly, prior year, net income incl. non-controlling interests, a 억/조 unit slip …) with the filing receipt number.

- **The model plans, the code decides.** The LLM only turns a sentence into a verification plan (company, metric, period, verbatim text spans). Parsing numbers, fetching filings, computing formulas, judging and citing are all deterministic, following [VeriFin](https://arxiv.org/abs/2608.10213): the claim never influences the facts or formula used to check it, and the verifier abstains when support cannot be established.
- **Three verdicts:** supported / contradicted / abstain. Abstain means "no basis to decide", not "wrong".
- **Korean-filing trap diagnosis:** consolidated vs separate, 3-month vs cumulative columns of quarterly reports, total vs owners' net income, prior year / prior quarter, 억·조 units, profit/loss sign, growth-formula slips, %p vs %.
- **Standard library only.** Nothing to install. You obtain your own free DART key and export it; the tool never stores or prints it.

> Unofficial; not affiliated with the papers' authors. "Supported" means consistency with the filing, not that the filing is right or that anything is investment advice.

## Install

1. Get a free key at [opendart.fss.or.kr](https://opendart.fss.or.kr) and set `DART_API_KEY` in your environment.
2. In Claude Code: `/plugin marketplace add sinkytiger/finground-lite-kr` then `/plugin install finground-lite-kr@finground-lite-kr`.
3. Paste a sentence with financial figures and ask for a fact-check; the `factcheck` skill takes over. Python 3.10+ must be runnable as `python`.

Without Claude Code: `python -m finground_kr.cli lookup 005930 2026 H1`, `python -m finground_kr.cli verify plans.json --as-of 2026-08-20`, `python -m finground_kr.cli serve`.

## Example

Sentence: "삼성전자의 2분기 영업이익은 146조7,252억원이었다" (article dated 2026-08-20)

| # | verdict | claim | filed value | difference | diagnosis | source |
|---|---|---|---|---|---|---|
| c1 | ❌ contradicted | 146조 7,252억원 | 89조 4,924억원 | -57조 2,327억원 | 2026 H1 cumulative value (146조 7,252억원) | 2026 half-year report (consolidated) IS 영업이익 thstrm (rcept 20260814003699) |

The sentence used the first-half cumulative figure where the three-month Q2 figure belongs. Tolerance is half a unit in the last digit the claim writes; the default mode accepts rounding or truncation and flags truncation, the strict mode accepts rounding only.

## Scope (v1)

Revenue, cost of sales, gross profit, operating income, net income (total and owners'), assets, liabilities, equity (total and owners'); operating/net/gross margin, debt ratio, equity ratio; quarterly (3-month), H1 and Q3 cumulative, Q4 (annual − Q3 cumulative), annual, quarter-end; year-on-year, quarter-on-quarter and vs-year-end growth, differences, %p changes, directions, profit/loss turns. Prior-year figures come from the same report's comparative columns. Reports filed after the article date are invisible. Out of scope (abstained): financial companies, non-December fiscal years, share prices and market cap, pre-restatement figures, preliminary earnings releases.

## Evaluation

**E1, verifier alone with gold plans.** A benchmark built in reverse from filed values (Samsung 2024 FY–2026 H1, the test fixtures): 934 sentences, 768 with injected trap errors; labels come from the tolerance rule only.

| mode | n | TA | FA | TR | FR | abstain | accepted-claim precision | accuracy | coverage |
|---|---|---|---|---|---|---|---|---|---|
| default (rounding or truncation) | 934 | 166 | 0 | 756 | 0 | 12 | 100% | 100% | 98.7% |
| strict (rounding only) | 934 | 145 | 0 | 777 | 0 | 12 | 100% | 100% | 98.7% |

All 12 abstentions are margin changes written in % instead of %p, which the verifier treats as ambiguous and reports both readings. Trap diagnosis found the expected reading in 488/488 trap samples; 10 of 268 near-miss samples (3.7%) coincidentally matched another reading. One company and template sentences: this shows the verifier behaves as designed, not its accuracy on real news. Full tables: [reports/e1_samsung_fixture.md](reports/e1_samsung_fixture.md).

**E2/E3 (in progress):** the full pipeline with Opus 5.5 writing plans, and Opus 5.5 as an LLM judge given the same filed figures (with and without the authorized formula), replicating VeriFin's "Judge+Formula" comparison on Korean data.

## Papers

VeriFin ([arXiv:2608.10213](https://arxiv.org/abs/2608.10213)) and FinGround ([arXiv:2604.23588](https://arxiv.org/abs/2604.23588), ACL 2026 Industry). Both target English SEC filings and have no usable public code (unlicensed / missing), so everything here is written from scratch. See [docs/papers.md](docs/papers.md) and [docs/design.md](docs/design.md) (Korean).

MIT license. DART data is subject to the OpenDART terms of use.
