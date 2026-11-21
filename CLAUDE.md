# Paysight: project guide for Claude Code

## What this is

Paysight is an AI payments analyst, built as a submission for the Pine Labs campus case study
(Case 2, "Enterprise AI Business Analyst"). A business user asks a plain-English question about
payment performance. Paysight then:

1. plans an analysis;
2. compiles and safety-checks SQL, and runs it on DuckDB;
3. performs deterministic statistical analysis;
4. returns an answer that separates facts, hypotheses and recommendations.

Every number in the answer is traceable to an executed query.

The full specification is in `docs/SPEC.md`. Read it completely before writing any code. If this
file and the spec disagree, the spec wins. If something is genuinely ambiguous, ask me instead of
guessing.

Deadline: public GitHub repo and demo video by 22 Nov 2026, 18:00 IST. Quality beats feature
count: a smaller system that is correct, tested and clearly explained beats a bigger, flaky one.

## Core principles (non-negotiable)

1. The LLM decides *what* to compute. Deterministic Python and SQL compute it. The LLM narrates
   *only* from computed results.
2. Every number shown to the user must trace to an evidence record (query, dataset, filters,
   time window). The grounding checker enforces this in code.
3. Safety is enforced in code, never only in prompts: read-only DuckDB with external access
   disabled, SQL AST validation, role-based allowlists and row filters, timeouts, row limits.
4. Facts, hypotheses, recommendations, caveats and assumptions are always separate and labelled.
5. Everything is deterministic: seeded data generation, a fixed `AS_OF_DATE`, and recorded LLM
   responses (replay mode). The demo and evals must reproduce exactly, including for a reviewer
   who has no API key.
6. Never fabricate. If data is missing, duplicated, conflicting or ambiguous, say so.
7. All banks, PSPs, wallets and merchants in the data are fictional. Never use real
   institution names, and never use Pine Labs logos or trademarks in the UI.

## Stack

- Python 3.11+, managed with `uv`.
- Data and analysis: DuckDB, pandas, numpy, scipy.
- SQL safety: sqlglot.
- Config and schemas: pydantic v2, PyYAML.
- UI: Streamlit, Plotly.
- CLI and plumbing: typer, tenacity, python-dotenv.
- LLM SDKs: google-genai, anthropic, openai. Each is optional at runtime and selected by env var.
- Tests: pytest. Lint and format: ruff.

## Commands

Keep this section accurate as you build. Every command must work on both Ubuntu 24.04 and
Windows 11: use `pathlib` everywhere, and expose every task as a `paysight` CLI command. A
Makefile may exist as a convenience, but must never be the only entry point.

```
uv sync
uv run paysight data generate            # builds data/paysight.duckdb + data/ground_truth.yaml
uv run paysight debug hero               # hero (B1) root-cause analysis without an LLM
uv run paysight ask "..." --role leadership
uv run streamlit run app/streamlit_app.py
uv run paysight eval run --mode replay
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

## Working rules

- Build milestone by milestone, as defined in `docs/SPEC.md` §17. After each milestone:
  1. Run ruff and pytest, and make sure everything passes.
  2. Commit with a conventional message (`feat:`, `test:`, `docs:`, `fix:`, `refactor:`).
  3. Print a short summary: what was built, what is tested, and any known gaps.
- Pause and wait for my confirmation after any milestone marked ⏸.
- Make small, meaningful commits, never one giant commit. Reviewers will read the history.
- Never commit secrets. Keys live in `.env` (gitignored), documented in `.env.example`.
- Do not commit the generated DuckDB file. The generator recreates it deterministically.
  Do commit replay cassettes, `data/ground_truth.yaml` and the eval reports.
- Type hints everywhere and docstrings on public functions. No dead code. No `TODO` without a
  matching entry in `docs/LIMITATIONS.md`.
- Prefer clear, boring code over clever code, and keep modules focused.
- If a spec decision turns out wrong in practice, do not deviate silently. Record the change and
  the reason in `docs/DECISIONS.md`.
- Do not touch files outside this repository.
