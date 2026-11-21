# Limitations

Honest limitations of Paysight. Every `TODO` in the code has a matching entry here.

## Data

- **Synthetic data.** All volumes, rates and scenarios are generated. Patterns are plausible
  but simpler than real payments data. For example, there are no chargebacks, no partial
  refunds at attempt level, and no merchant churn.
- **Scenario detectability at small scale depends on the seed.** At full scale, every scenario
  check passes for each seed tried (42, 1 and 3). At scale 0.1, sampling noise is about 1pp on
  daily SR, so borderline checks can fail for some seeds. Seen so far: S1's daily gap was
  1.8pp (seed 3, target ≥2pp), and S2's mix share was 0.91 (seed 1, target ≤0.85). The tests
  pin seed 42, which passes at both scales. The committed ground truth is always measured at
  full scale.

## Replay covers recorded questions only

- Every Appendix B question and every eval question (with its paraphrases) has a cassette,
  all from `claude-sonnet-5-5`. A new question, or a changed prompt or evidence bundle, needs
  one recording run with an API key (`LLM_MODE=record`); in replay mode it fails with a
  "No recorded response" message rather than guessing.

## Precision and hourly timelines

- **Mixed decimal places.** Most evidence stores percentages as numbers rounded to two decimals,
  so 89.40 is sent as 89.4, and an answer can show "89.4%" next to "89.45%". New
  segment-comparison evidence uses fixed two-decimal text (D36). Changing the older evidence
  would invalidate every recorded narration, so it is left for the next re-recording.
- **Daily summaries are not everywhere yet.** Daily series that answer *when* (anomaly, trend,
  ranking drill-down) carry a computed summary (D37). The root-cause segment series and
  LLM-suggested daily series still give the narrator only their first 12 rows plus the anomaly
  windows; summarising them would change ten recorded root-cause answers, which was deferred
  to keep spend within budget.
- **Hourly outages need a reference period.** Outage windows are judged against the week
  before the drop. A drop in the first week of the data has no such week, so no hourly
  outage is computed for it.

## Context dimensions

- Only `card_bin` declares a context dimension (`issuer_name`, D34). `merchant_name` →
  `merchant_group` would fit the same rule, but adding it changes several recorded answers and
  needs a re-recording run, so it was left for later.

## Evaluation

- **Our own questions, on our own data.** The 39 questions, their gold SQL and the paraphrases
  were written by us against scenarios we planted. The results show that the system finds what
  it was designed to find; they do not measure accuracy on real enterprise data or on
  questions written by someone else.
- **A deliberately naive baseline.** It shares the model, validator and role filters but has
  no semantic layer, playbooks, statistics or grounding. A stronger baseline (for example, one
  with metric definitions in its prompt) would narrow the gap.
- **One model.** Every recording uses `claude-sonnet-5-5`. Other providers are implemented and
  unit-tested, but they are not evaluated.
- **Latency.** Replay latencies exclude LLM time. Live latency is roughly the intent call plus
  the narration call, with an extra narration when grounding needs a regeneration.

## Out of scope

- **Real authentication.** The role selector simulates SSO; anyone running the app can pick
  any role.
- **Multi-tenancy.** One dataset and one set of policies.
- **Streaming or real-time data.** A fixed three-month snapshot with `AS_OF_DATE` = 31 Oct 2026.
- **A production warehouse.** DuckDB runs locally. See "Path to production" in the README.
- **Write-back of any kind.** Paysight is read-only by design and refuses write requests.
- **Real PII handling beyond allowlisting.** All data is synthetic; card BINs are fictional
  (they start with 9) and customer references are hashes. Column sensitivity is enforced by
  allowlists, not by masking or tokenisation.
- **Causal inference.** Decomposition and driver ranking attribute a change to segments; they
  do not prove why it happened. Causes are offered as hypotheses, each with a way to verify it.
- **Guaranteed LLM correctness.** Structured outputs, validation and grounding reduce errors
  but do not eliminate them. Grounding checks that numbers come from evidence, not that the
  reasoning connecting them is right, and the ad-hoc SQL path can still answer a slightly
  different question than the one asked (see D34).
