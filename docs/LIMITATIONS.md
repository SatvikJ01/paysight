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

## Context dimensions

- Only `card_bin` declares a context dimension (`issuer_name`, D34). `merchant_name` →
  `merchant_group` would fit the same rule, but adding it changes several recorded answers and
  needs a re-recording run, so it was left for later.
