# Paysight

> An AI payments analyst that shows its work and catches the traps naive AI falls into.

Submission for the Pine Labs campus case study, **Case 2: Enterprise AI Business Analyst**.
A business user asks a plain-English question about payment performance. Paysight plans an
analysis, compiles and safety-checks SQL, runs it on DuckDB, performs deterministic statistical
analysis, and answers with facts, hypotheses and recommendations kept separate. Every number in
the answer traces to an executed query.

All banks, PSPs, wallets and merchants in the data are fictional.

> **Status:** under construction. See `docs/SPEC.md` for the full specification.

## Quickstart (no API key needed)

```bash
uv sync
uv run paysight data generate      # builds data/paysight.duckdb (~15s) and checks every planted scenario
```

The data is synthetic, deterministic and fictional. See [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)
for the schema and the planted-scenario answer key.

## License

MIT. See [LICENSE](LICENSE).
