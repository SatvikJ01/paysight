# Submission checklist (SPEC §18.3)

Checked on 21 Nov 2026 against commit `HEAD` of `main`. Each item lists how it was verified.

- [x] **A fresh clone, following the README Quickstart exactly, works with no API key.**
  Cloned into an empty temporary directory, with no `.env` and no key in the environment.
  Then, in the clone: `uv sync`; `uv run paysight data generate` (14 s, all scenario checks
  PASS, working tree still clean afterwards); `uv run streamlit run app/streamlit_app.py`.
  Headless Chrome opened all 15 Appendix B cases (B1–B12 plus the three B3 choices) on that
  server and found the expected content in each. The other README commands also ran in the
  clone: `paysight ask` (hero answer, replay), `paysight debug hero`,
  `paysight eval run --mode replay` (97.2% vs 33.3%, identical to the committed report apart
  from timestamp and latency), and `pytest` (306 passed).
- [x] **Docker works with no API key.** `docker build -t paysight .` builds (the build runs
  `paysight data generate`, and every scenario check passes). `docker run --rm -p 8501:8501
  paysight` has no `.env` and no key in the environment, reports `healthy` through its
  `HEALTHCHECK`, and passes the same headless check: 15 of 15 Appendix B cases render.
  `docker exec … paysight ask` answers in replay mode. The final check rebuilt the image from
  a fresh clone at commit `335301d` and repeated these checks with the same results. The image
  is 2.5 GB, mostly pyarrow, scipy and the three LLM SDKs.
- [ ] **CI is green. All tests pass. ruff is clean.** Locally on Ubuntu 24.04: `pytest -q`
  gives 306 passed; `ruff check .` and `ruff format --check .` are clean. CI
  (`.github/workflows/ci.yml`) runs the same steps on Ubuntu and Windows on push. These
  commits have not been pushed yet, so CI, and the Windows run in particular, is unverified
  until then.
- [x] **No secrets in the repo or its git history.** `.env` has never been committed
  (`git log --all -- .env` is empty). A scan of every diff in all 56 commits finds no
  Anthropic, Google, OpenAI or GitHub key patterns and no private keys, and neither local key's
  prefix appears anywhere in history.
- [x] **Every Appendix B question works in replay mode in the UI.** 15 of 15 cases pass the
  headless UI check above. `tests/test_replay.py` also replays all of them in CI, with the
  grounding guarantee asserted on every answer.
- [x] **Every core requirement and failure scenario appears in the README coverage tables.**
  All 7 core and 6 stretch requirements from Appendix A, and all 6 failure scenarios, each with
  its implementation, a test and a demo question.
- [x] **The eval report is committed and embedded.** `eval/reports/latest.md` and `.json` are
  committed and reproduce from cassettes. The README's Evaluation section embeds the summary
  table, states the limits, and links the full report.
- [x] **Screenshots are present. The README renders correctly on GitHub, including Mermaid.**
  Eight 1600px-wide captures in `docs/images/`, taken with headless Chrome after Streamlit
  finished rendering. Every relative link and image in the README and docs resolves. Both
  Mermaid diagrams parse and render with Mermaid 11. GitHub's own rendering can only be checked
  after the push.
