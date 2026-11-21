# Decisions

Short ADR-style records. Each entry states the decision, why it was made, and what it changes
relative to `docs/SPEC.md`, if anything.

## D1. "Extra failure probability" is a hazard applied before the baseline draw

**Context.** S1, S3 and S4 say an attempt gets "an extra *p* probability" of a failure code. That
could mean an additive drop in SR or an independent extra failure.

**Decision.** Each scenario is a *hazard*. The outcome model draws it first. With probability *p*
the attempt fails with the scenario's code; otherwise the normal success draw runs. The affected
SR becomes `p_base × (1 − p)`, and about *p* of affected attempts carry the scenario code, which
keeps the failure-code shift crisp.

**Spec impact.** None. This is an interpretation.

## D2. NovaMart uses channel-blended per-method success rates (S2)

**Context.** The spec says NovaMart's per-method success rates "equal everyone else's". NovaMart
is online only, while other merchants' POS attempts get +3pp. The first build used NovaMart's
plain *online* rates. Its sale then also lowered each card method's blended rate, by about
1pp within CREDIT_CARD and CARD_EMI. That landed in the **rate** effect of a decomposition by
payment method. Realized mix share was **57.6%**, below the 60–85% target.

**Decision.** Each NovaMart attempt gets the *channel-blended* POS bonus for its method: the
expected bonus given the overall channel mix of that method. Its per-method rates then equal the
all-merchant rates, which is the literal requirement, and the sale is a pure mix shift.

**Result.** Mix −1.00pp, rate −0.47pp, mix share **68%** (full scale, seed 42). This matches the
spec's back-of-envelope estimate (≈1.1pp and ≈0.4pp). Harbor debit (S3) is now 84% of the rate
effect.

**Spec impact.** None to the targets. This is a change to S2's modelling, not to any range.

## D3. "About 11,000 attempts per day" counts first attempts

Retries (S8) and NovaMart's festive volume (S2) come on top. The total is 1.15M attempts, within
the spec's 1.0–1.3M range.

## D4. How "detectable" is operationalized for each scenario

The spec gives some expectations qualitatively. The generator measures each one with SQL and
checks it as follows (see `src/paysight/data/ground_truth.py`):

- **S1.** The trailing 7-day median is the median of the daily attempt SR over 10–16 Sep.
- **S3, "bulk of the rate effect".** Decompose the S2 comparison by issuer × payment method. Harbor
  debit's rate contribution must be at least 50% of the total rate effect.
- **S4, "clearly visible on a heatmap".** Zephyr UPI evening SR (19–22h) on 1–10 Sep must be at
  least 7pp below the same hours on reference dates (22–31 Aug and 11–20 Sep). Daytime hours must
  be within 2pp, so the effect is specific to the evening.
- **S8, "several pp".** Order SR must be at least 2pp above attempt SR over the full period.

## D5. Kitagawa decomposition is implemented in M1

The realized ground truth for S2 and S3 needs the decomposition, so
`paysight.analysis.decompose` was written in M1 and is reused by the M3 toolkit, not duplicated.

## D6. Determinism and the content hash

Each random component draws from its own stream, seeded by `(seed, crc32(component name))`.
Tuning one scenario therefore never re-randomizes another. The content hash covers every data
table but not `meta_data_version.generated_at`, which records the wall-clock build time. A slow
test rebuilds the full-scale dataset and checks it reproduces the committed hash.

## D7. Settlement modelling choices

- UPI has 0% MDR (as for UPI P2M in India). Cards are 1.6%, netbanking 1.2% and wallets 1.5%.
- S7's missing batch is applied per NovaMart merchant. Each one's 25 Oct and 26 Oct batches are
  combined and settled on 28 Oct.
- Settlements are only generated up to `AS_OF_DATE`, so 31 Oct transactions are unsettled. This is
  a legitimate timing difference that the reconciliation must explain.

## D8. Fictional card BINs start with 9

Real BIN ranges map to real issuers. Every generated BIN starts with `9`, a range the major card
networks don't use, so no BIN in the data identifies a real institution.

## D9. Negative filters are NULL-safe

**Context.** `merchant_group <> 'StellarFuel'` is NULL, and therefore false, for every merchant
without a group. On the settlements data it kept 722 of 35,523 batches. A user asking for
"everything except StellarFuel" would silently lose 98% of the data.

**Decision.** The compiler emits `IS DISTINCT FROM` for `!=` and `(NOT col IN (...) OR col IS NULL)`
for `not_in`. A regression test checks that "all minus StellarFuel" equals "everything minus
StellarFuel".

## D10. What counts as a valid time-window predicate

Every SELECT scope that reads `v_attempts_clean` or `v_orders` directly needs literal lower and
upper bounds on that source's `created_at`. The bounds must be top-level `AND` conjuncts of the
scope's own WHERE clause: `>=`/`>`, `<`/`<=`, `BETWEEN` or `=`, against a string or `CAST`
literal. The span may be at most 400 days. This is deliberately strict:

- a bound inside an `OR` does not count;
- a bound computed with `now()` does not count;
- a bound in an outer query does not cover a subquery that reads the view again.

The compiler satisfies the rule by emitting an envelope predicate even for comparison queries.
`v_settlements` is not a fact view and needs no time predicate.

## D11. Comparison windows compile to a single query

A `QuerySpec` with `compare_window` compiles to one SELECT with a `comparison_window` column
(`baseline` / `current`). The WHERE clause holds an envelope over both windows, plus an `OR` of
the two windows when they are not adjacent. Every segment's numerator and denominator for both
windows therefore come from one validated query, which is exactly what the Kitagawa
decomposition and the z-tests need.

## D12. Validator details

- **Check order.** The report lists checks in spec order. The time-window check is computed on
  the query *before* the row-level rewrite, so the user's own predicate is what gets checked.
- **Read-only allowlist.** Only `SELECT` and set operations are allowed at the root, and a
  denylist of dangerous node types applies anywhere in the tree. Statements sqlglot cannot type
  (it parses `LOAD`/`RESET`/`VACUUM` as `Command`, and a bare `CHECKPOINT` as a column) are
  rejected by the allowlist. Statements it cannot parse (`EXPORT`/`IMPORT DATABASE`) fail the
  parse check.
- **Case-insensitive names.** Table names are matched case-insensitively, as DuckDB does.
  CTE names may not shadow real tables or views.
- **No governed view.** A query that reads no governed view (for example `SELECT * FROM
  range(3)`) passes. It can reveal nothing.
- **Joins.** `NATURAL JOIN` is rejected even though its condition is implicit. Every join must
  state its condition.
- **Hardening is verified, not assumed.** All in-process connections to one DuckDB file share a
  database instance, so the engine checks that `enable_external_access` is off and
  `lock_configuration` is on. If not, it refuses to start.
- **Raw tables.** The raw tables are never reachable through the validator. The data-quality
  tool (M3) runs fixed, reviewed SQL through a separate internal path.

## D13. Per-segment attribution uses centred mix terms

**Context.** The spec's per-segment mix term `(s_i1 − s_i0)·(r_i0 + r_i1)/2` is exact in total,
but the individual terms are huge and offsetting. On the hero data, UPI shows −10.6pp and EMI
+6.5pp of "mix" for a total change of −1.47pp. A waterfall of those bars would mislead.

**Decision.** Keep the spec terms. Also compute `mix_centered_i = Δs_i·(r̄_i − R̄)` with
`R̄ = (R_0 + R_1)/2`. Because shares sum to 1 in both windows, `Σ mix_centered = Σ mix` exactly
(tested). The centred term answers "how much did this segment's share change move the overall
rate?": EMI −0.61pp and UPI −0.43pp on the hero data. Ranking and charts use it.

## D14. Driver strength measures disproportionality, not size

**Context.** The first implementation ranked dimensions by their largest single-segment
contribution. `merchant_tier` came first, because enterprise merchants are most of the volume
and so "explain" most of any change. `payment_method`, the real mix story, came 5th.

**Decision.**
- **Strength** is the total-variation distance between a dimension's attribution and its
  volume shares: `½·Σ|a_i − s̄_i·ΔR| / |ΔR|`. A uniform change scores about 0 (tested). On the
  hero data the ranking is merchant_group 0.47, payment_method 0.47, issuer_name 0.27.
- **Rate-effect localization.** Decompose by `payment_method × dimension`, so mix inside a
  segment counts as mix and what remains is a genuine rate change. This correctly isolates
  Harbor Bank | DEBIT_CARD (86.1% → 76.7%, q ≈ 1e-49), not NovaMart. NovaMart's own rate dip is
  its EMI-heavy mix.

## D15. Low-volume periods do not end an anomaly window

Periods with too few attempts to judge (for example night hours of an hourly series) are
treated as unknown, not normal. Without this the Kestrel outage split into two windows at
night. With it, the detected window is exactly 17 Sep 10:00 → 18 Sep 22:00.

## D16. Data-quality checks use an internal, reviewed-SQL path

**Raw-table access.** The data-quality tool must read the raw table to see duplicates. It does
so through `Engine.run_internal`, which accepts only module-level SQL constants with bound
parameters. Evidence records it as `internal_reviewed_query`. The connection is the same
hardened, read-only one, and the timeout still applies. The role's row filters are applied
through `dim_merchant`, so a regional analyst sees only their region's findings.

**Daily volume.** Volume is compared with the same weekday's median rather than a global median,
because weekends run about 15% higher. Spikes are reported as `info` (a sale is not a data
error) and drops as `warning`.

## D17. Reconciliation flags individual batches, not only the window total

Orbit's 1.8% shortfall on one day is only −0.22% of its weekly GMV, below the 0.5% flag
threshold. Each batch is therefore also checked against the GMV of its own covered dates, and
any batch at or above the threshold is flagged as unexplained. "Missing batch" (no batch
covers a date and none is due) is reported as explained but marked for follow-up.

## D18. LLM layer details

- **Schema validation lives in one place.** Providers return raw JSON text. Malformed JSON and
  schema mismatches are both pydantic validation errors, and share the single re-ask path.
- **Retries belong to tenacity, not the SDKs.** SDK-level retries are disabled
  (`max_retries=0`), so every call has at most 4 attempts, retried only on 429, 5xx or a
  timeout, with jittered exponential backoff.
- **HTTP stack.** The pinned SDK versions use `httpx2`, a fork of `httpx`, not `httpx`.
  Transport-error detection checks whichever of the two is installed. The stubbed provider
  tests caught the original `import httpx`, which would have crashed on the first transient
  error.
- **Temperature.** OpenAI requests omit `temperature`, because several current OpenAI models
  accept only the default. Gemini and Anthropic use temperature 0.
- **Default models.** `gemini-3.8-flash` (the current stable Flash model, pinned rather than
  `gemini-flash-latest` so recordings stay reproducible), `claude-sonnet-5-5` and `gpt-5-mini`.
  They are set only in `config/settings.yaml` and documented in `.env.example`.
- **Cassette fallback.** In live mode, if the provider is unavailable after its retries and a
  cassette exists for the exact prompt, the cassette is used and labelled `replay-fallback`.

## D19. Two LLM calls per question; extra steps ride on the intent call

The spec allows the LLM to append up to 3 plan steps. Instead of a separate planning call, the
intent schema has an `extra_steps` field. The planner validates it: steps come from a fixed tool
list, PII dimensions are dropped, and at most 3 are kept. Each kept step is marked
"LLM-suggested". A normal question costs two calls (intent and narration). A third happens only
when the first narration fails grounding.

## D20. Unsafe requests: deterministic guard first, LLM second

A regex guard (DML or deletion of rows/tables, "ignore your instructions", requests for system
prompts or keys) refuses before any LLM call or query. The LLM's `unsafe_request` class catches
what the patterns miss. B9 is refused by the guard, so it needs no cassette. The patterns are
deliberately narrow: "why did success rate *drop*" is not caught.

## D21. Clarification options are fixed, not generated

A why/compare/trend/anomaly question without a period gets three fixed options (last 7 vs
previous 7, last 14 vs previous 14, October vs September). Choosing one applies a structured
override to the same parsed intent. No new LLM call is needed, so every button replays from the
same cassette set. Unresolved entity names produce "did you mean" options from fuzzy matching
against real dimension values.

## D22. Few-shot examples are paraphrases of the demo questions

The spec asks for few-shot examples taken from the demo questions. Using them verbatim would
leak the evaluation set into the prompt, so the examples teach the same output shapes with
different wording and values (three weeks instead of two, "orders" instead of "attempts", and
so on).

## D23. Grounding rules in practice

- **Display precision sets the tolerance.** A claim matches if it is within half a unit of its
  last displayed digit: "88.5%" matches 88.54, but "88.50%" does not.
- **What counts as a claim.** Bare decimals are claims. Small bare whole numbers under 100
  ("top 5"), clock times and unit phrases ("per 100 attempts") are not. The first live run
  showed "per 100 attempts" being read as a count of 100.
- **Facts must cite.** A fact must cite evidence, and its numbers must be in *that* evidence.
  The headline may draw on any evidence.
- **One regeneration.** After a failure the narrator is re-asked once with the specific
  failures. Whatever still fails is removed and listed as "Unverified claims (removed)". An
  unverifiable headline is replaced by the first verified fact.
- **Display units.** Rates go to the narrator as percentages with two decimals, so it copies
  rather than converts.

## D24. Confidence wording

Confidence starts at High, and each concern caps it, adding a reason. Concerns are: small
samples, a change that is not significant (statistically or practically), an unstable mix
split, data-quality findings in scope, ad-hoc SQL, assumed defaults, and removed claims.
Warning-level data-quality findings that Paysight corrects (duplicates removed) still cap at
Medium, as in the spec's example. Info-level findings, such as a volume spike during a sale,
do not cap.

## D25. `record` mode never overwrites an existing cassette

**Context.** LLM output is not perfectly deterministic, even at temperature 0. While recording
Appendix B, the B3 question and each of its clarification choices called Gemini live for the
same intent prompt. Each call returned a slightly different restatement and overwrote the one
intent cassette. The narrator cassettes recorded for the earlier intents were orphaned: their
prompts no longer occur on replay.

**Decision.** In `record` mode, a prompt that already has a cassette is replayed rather than
re-requested, so a recording session is internally consistent and spends quota only on new
prompts. `PAYSIGHT_RERECORD=1` forces fresh responses, for example after a prompt change.
This deviates from the spec's "record: call live and save" only for prompts that are already
recorded.

## D26. Provider errors never escape the LLM layer

Non-retryable provider errors (bad requests, auth failures, an exhausted daily quota) are
wrapped as `LLMUnavailableError`, like exhausted retries. The pipeline therefore reports which
step failed and still shows the evidence computed so far, instead of crashing (SPEC §12.2).
A daily-quota 429 is not retried, because it cannot recover within the retry window.

## D27. Claude request shape: structured outputs, no temperature, thinking off, cached prefix

**Model.** All cassettes, the Paysight eval and the baseline use `claude-sonnet-5-5`: one model,
chosen from a measured cost estimate. The Gemini cassettes are replaced.

**Request shape.**
- **Structured outputs.** Forcing a tool call with `tool_choice` returns a 400 on Sonnet 5.5 and
  Opus 5.5, so the provider sends the pydantic schema as `output_config.format`, converted with
  the SDK's `transform_schema` (strict: every property required, `additionalProperties: false`).
- **No `temperature`.** Current Claude models reject non-default sampling parameters.
  Determinism comes from cassettes, not sampling settings.
- **Thinking.** Thinking is billed as output and is not needed to fill a schema or narrate
  precomputed evidence: `between_tools` on Sonnet 5.5, `disabled` on Haiku 5.5, and low effort
  on Opus 5.5, where it cannot be turned off.
- **Prompt caching.** A `cache_control` breakpoint sits on the system block, which is static per
  template. The intent system prompt (catalog plus examples) is about 98% of that request.
- **No server-side refusal fallback.** It is not enabled, because it would answer with a
  different model and break the one-model rule. A refusal surfaces as an `LLMError`, and the
  pipeline degrades gracefully.

## D28. A persistent spend ledger enforces the LLM budget

`runs/llm_spend.json` accumulates the cost of every live call: input, output, cache writes and
cache reads, priced per model from `config/settings.yaml`. It persists across separate CLI runs.
Before each live call the client refuses if spend has reached `llm.budget_usd` ($4). After each
call, the response is saved first and then the ledger raises if the new total went over, so
recording or an eval stops at the first call that crosses the budget.

## D29. When the ad-hoc SQL fallback runs

There are two triggers.
- **The intent says so.** `needs_custom_sql` is set when a calculation is outside the catalog:
  a median, a share of orders meeting a condition, distinct counts, a segment compared with the
  overall figure.
- **A compile error.** A playbook raises `NotExpressibleError` when a spec cannot compile.

**What the LLM sees.** Only the views the role may query, with columns of denied sensitivity
left out of the schema. **Repair.** One attempt, given the validator's exact failures. Both
attempts' validation reports are kept on the evidence, which is tagged `ad_hoc` and caps
confidence at Medium. A caveat says the SQL was AI-written and validated, but not governed.

**The repair eval case.** It asks for "the past two years". The prompt states the time-bound
rules but not the validator's 400-day maximum, so a natural first SQL fails validation and must
be repaired. The prompt does not withhold any rule in order to force failures.
