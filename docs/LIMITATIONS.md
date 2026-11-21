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

## Recording status (temporary)

- Cassettes exist for B1, B2 and the B3 clarification; B9 needs none. The remaining Appendix B
  questions and the B3 choice narrations still have to be recorded. The Gemini free tier
  stopped recording at 20 requests per day. Until they are recorded, those questions fail in
  replay with a "No recorded response" message, and one replay test is skipped.
