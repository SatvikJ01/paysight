"""Command-line entry point: ``paysight``.

Every project task is exposed here so it runs identically on Linux and Windows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from paysight import __version__
from paysight.config import get_settings

app = typer.Typer(
    name="paysight",
    help="Paysight: an AI payments analyst that shows its work.",
    no_args_is_help=True,
    add_completion=False,
)
data_app = typer.Typer(help="Generate and inspect the synthetic dataset.", no_args_is_help=True)
app.add_typer(data_app, name="data")


@app.command()
def version() -> None:
    """Print the Paysight version."""
    typer.echo(__version__)


@app.command("config")
def show_config() -> None:
    """Print the resolved settings (paths, dates, limits and thresholds)."""
    settings = get_settings()
    typer.echo(settings.model_dump_json(indent=2))


def _print_checks(truth: dict) -> bool:
    """Print a table of every scenario check; return whether all passed."""
    from paysight.data.ground_truth import iter_checks

    rows = iter_checks(truth)
    w1 = max(len(s) for s, _, _ in rows)
    w2 = max(len(n) for _, n, _ in rows)
    typer.echo(f"{'section':<{w1}}  {'check':<{w2}}  {'realized':>10}  {'expected':<14}  ok")
    typer.echo("-" * (w1 + w2 + 34))
    for section, name, check in rows:
        mark = "PASS" if check["passed"] else "FAIL"
        typer.echo(
            f"{section:<{w1}}  {name:<{w2}}  {check['value']!s:>10}  {check['expected']!s:<14}  "
            f"{mark}"
        )
    return all(c["passed"] for _, _, c in rows)


@data_app.command("generate")
def data_generate(
    scale: Annotated[float, typer.Option(help="Volume multiplier (tests use 0.1).")] = 1.0,
    seed: Annotated[int | None, typer.Option(help="Random seed (default from settings).")] = None,
    out: Annotated[Path | None, typer.Option(help="DuckDB output path.")] = None,
    ground_truth: Annotated[
        Path | None, typer.Option(help="Where to write the realized ground truth YAML.")
    ] = None,
) -> None:
    """Build the DuckDB dataset and write the realized ground truth."""
    from paysight.data.generator import generate
    from paysight.data.ground_truth import write_ground_truth

    settings = get_settings()
    d = settings.data
    out_path = out or settings.duckdb_path
    truth_path = ground_truth or settings.ground_truth_path
    result = generate(
        out_path,
        start=d.start_date,
        end=d.end_date,
        as_of=d.as_of_date,
        seed=d.seed if seed is None else seed,
        scale=scale,
    )
    write_ground_truth(result.ground_truth, truth_path)
    typer.echo(
        f"Wrote {result.path} ({result.raw_rows:,} raw attempts, "
        f"{result.settlement_rows:,} settlement batches) in {result.seconds:.1f}s"
    )
    typer.echo(f"Content hash {result.content_hash[:16]}…  Ground truth: {truth_path}\n")
    if not _print_checks(result.ground_truth):
        typer.echo("\nWARNING: some scenario checks failed; see the table above.", err=True)
        raise typer.Exit(code=1)


@data_app.command("truth")
def data_truth(
    path: Annotated[Path | None, typer.Option(help="Ground truth YAML to read.")] = None,
) -> None:
    """Print the scenario check table from an existing ground-truth file."""
    from paysight.data.ground_truth import load_ground_truth

    truth = load_ground_truth(path or get_settings().ground_truth_path)
    if not _print_checks(truth):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
