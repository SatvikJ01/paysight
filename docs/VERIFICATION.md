# Verification log

How Paysight was checked, and the evidence for each check. The checks follow SPEC §18.3; they
were last run on 21–22 Nov 2026.

## Scripted checks

- **Fresh clone, no API key: passed.** Cloned into an empty temporary directory, with no
  `.env` and no key in the environment. Then, in the clone: `uv sync`;
  `uv run paysight data generate` (14 s, all scenario checks PASS, working tree still clean
  afterwards); `uv run streamlit run app/streamlit_app.py`. Headless Chrome opened all 15
  Appendix B cases (B1–B12 plus the three B3 choices) on that server and found the expected
  content in each. The other README commands also ran in the clone: `paysight ask` (hero
  answer, replay), `paysight debug hero`, `paysight eval run --mode replay` (identical to the
  committed report apart from timestamp and latency), and `pytest`. The author repeated the
  fresh clone independently (see Manual verification below).
- **Docker, no API key: passed.** `docker build -t paysight .` builds (the build runs
  `paysight data generate`, and every scenario check passes). `docker run --rm -p 8501:8501
  paysight` has no `.env` and no key in the environment, reports `healthy` through its
  `HEALTHCHECK`, and passes the same headless check: 15 of 15 Appendix B cases render.
  `docker exec … paysight ask` answers in replay mode. The image was rebuilt from a fresh
  clone at commit `335301d` and the checks repeated with the same results. The image is 2.5 GB,
  mostly pyarrow, scipy and the three LLM SDKs.
- **Tests and lint: passed locally.** On Ubuntu 24.04: `pytest -q` gives 351 passed;
  `ruff check .` and `ruff format --check .` are clean. CI (`.github/workflows/ci.yml`) runs the
  same steps on Ubuntu and Windows; its latest result, the Windows run in particular, is not
  recorded here.
- **No secrets in the repository or its history: passed.** `.env` has never been committed
  (`git log --all -- .env` is empty). A scan of every diff in all 72 commits finds no
  Anthropic, Google, OpenAI or GitHub key patterns and no private keys, and neither local key's
  prefix appears anywhere in history.
- **Every Appendix B question works in replay mode in the UI: passed.** 15 of 15 cases pass the
  headless UI check above. `tests/test_replay.py` also replays all of them in CI, with the
  grounding guarantee asserted on every answer.
- **Coverage tables: complete.** The README maps all 7 core and 6 stretch requirements from
  Appendix A, and all 6 failure scenarios, to their implementation, a test and a demo question.
- **Evaluation report reproduces: passed.** `eval/reports/latest.md` and `.json` reproduce from
  the cassettes; two consecutive replays are identical apart from timestamp and latency. The
  README's Evaluation section embeds the summary table and states the limits.
- **Screenshots and rendering: passed locally.** Nine 1600px-wide captures in `docs/images/`,
  taken with headless Chrome after Streamlit finished rendering. Every relative link, anchor
  and image in the README and docs resolves. Both Mermaid diagrams parse and render with
  Mermaid 11; GitHub's own rendering is not recorded here.

## Manual verification (21 Nov 2026, by the author)

Trace files for every run are in `runs/<run id>.json`.

**Reviewer path (passed).** A fresh clone from GitHub into an empty temporary directory, with
no `.env` and no key: `uv sync`, `paysight data generate` and `streamlit run` all worked, and
the badge showed REPLAY. Every Appendix B example (B1–B12, plus a B3 choice) behaved as
expected, each with the confidence badge and the Governed / Ad-hoc SQL badge. The Data health,
Eval scorecard and Semantic catalog pages loaded cleanly. `git check-ignore .env` printed
`.env`.

**Live tests with `claude-sonnet-5-5`:**

| Run id | Role | Question | Result |
|---|---|---|---|
| `20261121-190757-2f3bff` | leadership | Which region had the lowest success rate in September? | Correct; noted the narrow 89.4–89.84% band. Cosmetic: "89.4%" next to "89.45%" (see C below). |
| `20261121-190930-89a80d` | leadership | Top 5 merchants by GMV in October | Correct and well caveated. |
| `20261121-191009-cfa116` | leadership | What's the weather in Mumbai? | Correctly refused as out of scope. |
| `20261121-191024-a3bf0d` | analyst_north | Show me everything about region south | Correctly denied; no query ran. |
| `20261121-190907-7fe08b` | leadership | Compare UPI and card success rates in August | **Bug 1** (below). |
| `20261121-191712-6c9a14` | leadership | Which acquiring bank had the most technical payment failures in September, and when did they happen? | **Bug 2** (below). |

**Bug 1: a segment comparison treated as a period comparison.** The intent was right
(`comparison`, `method_family in [upi, card]`, August, no second period), but the planner gave
every comparison a previous-period baseline, so it looked for July (outside the data). It then
offered clarification options that ignored the stated period. *Fixed (D36):* a comparison that
names two or more segments and no second period compares the segments within the window (rates
with numerator and denominator, Wilson intervals, a two-proportion test, a bar chart). When a
requested comparison period is outside the data, Paysight says the data starts on 1 Aug 2026
and offers only options that keep the stated period. Replayed answer: UPI 93.1% vs card 86.6%
in August, a 6.52pp gap, statistically and practically significant.

**Bug 2: a ranking that ignored "when".** The intent asked for daily, hourly and failure-code
steps, but the ranking playbook never ran extra steps, so the answer said it could not tell
when, and the Charts tab was empty. *Fixed (D36):* when the intent asks for timing, the ranking
drills into the top segment (daily rate with anomaly windows, then the anomaly playbook's
explanation of the main drop). Count rankings now show each segment's failure rate. Replayed
answer: Kestrel Bank, 2,776 technical failures (2.94% of its attempts against about 2.1–2.2% for
the others), concentrated on 17–18 September, when its card methods fell 24–30pp and
`ACQ_TIMEOUT` rose from 0.26 to 32.27 per 100 attempts.

**C: precision.** New segment-comparison evidence uses fixed two-decimal rates. The older
evidence keeps its format so that recorded answers stay unchanged (LIMITATIONS.md).

**Tests and eval added:** segment comparisons (UPI vs card in August and in October), a period
comparison that still compares periods, the clarification that keeps an explicit period
(including both choices), a ranking with "when" that drills down, and a plain ranking that does
not. Three eval cases with gold SQL cover these behaviours; all three pass for Paysight.

### Live re-runs after the fixes (21 Nov 2026)

| Run id | Question | Result |
|---|---|---|
| `20261121-203507-cde2d0` | Compare UPI and card success rates in August | Correct: 93.1% vs 86.6%, 6.52pp, intervals, z-test, Meridian data-quality caveat. |
| UI live run (no run id noted) | Which acquiring bank had the most technical payment failures in September, and when did they happen? | Correct ranking with failure rates, 17–18 Sep card-only, `ACQ_TIMEOUT` 0.26 → 32.27 per 100 attempts, High confidence, three charts. |

Three issues remained and were fixed on the `final-polish` branch (D37):

1. **False-positive anomaly.** The daily detector flagged Kestrel on 4 Sep (about −1.3pp) and
   the headline called it a second dip. Anomalies now also need at least 2pp of practical
   size; the replayed answer reports only the 17–18 Sep outage.
2. **Truncated evidence.** The narrator saw only the first hours of the hourly series. The
   outage window is now computed in code against the week before (17 Sep 10:00 to 18 Sep
   22:00, 36 hours, worst hour 36.36% against 86.87%) and shaded on the chart; daily series
   that answer "when" carry a computed summary.
3. **"p=0.0".** Tiny p-values display as "p < 0.001".

## Independent code review (22 Nov 2026)

An independent code review of commit `a87fa55` reported seven issues
([D38](DECISIONS.md#d38-fixes-from-an-independent-code-review)). Six were reproduced with a
failing regression test and fixed: reconciliation scope (NovaMart, north, 25–31 Oct now gives
₹3.99 crore on both sides), decomposition of the reported metric with a sum-to-headline
invariant, ranking direction and count from the question, grounding of change direction,
bar charts under pandas 3, and the driver metric's name. The seventh ("revenue" read as GMV)
would change the intent prompt and is recorded in LIMITATIONS.md. After every fix, all 70
replayed Appendix B and eval answers (with paraphrases) were compared with a snapshot taken
before the work: none changed, and the replayed eval matched the committed report apart from
the renamed metric.
