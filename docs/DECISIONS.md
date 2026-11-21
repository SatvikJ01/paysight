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
