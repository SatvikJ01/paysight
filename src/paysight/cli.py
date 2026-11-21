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
debug_app = typer.Typer(help="Run analyses directly, without an LLM.", no_args_is_help=True)
app.add_typer(debug_app, name="debug")


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


@debug_app.command("hero")
def debug_hero(
    role: Annotated[str, typer.Option(help="Role to run as.")] = "leadership",
) -> None:
    """Run the hero root-cause analysis (B1) deterministically and print the numbers."""
    from paysight.analysis.compare import compare_periods, denominator_sensitivity
    from paysight.analysis.drilldown import failure_code_shift
    from paysight.analysis.drivers import localize_rate_effect, scan_drivers
    from paysight.execution.engine import Engine
    from paysight.safety.access import get_policies
    from paysight.semantic.dates import resolve, resolve_compare
    from paysight.semantic.loader import get_semantic_layer
    from paysight.semantic.models import Filter

    settings = get_settings()
    d, a = settings.data, settings.analysis
    policy = get_policies()[role]
    engine = Engine(settings.duckdb_path, layer=get_semantic_layer(), limits=settings.limits)
    current = resolve("last_n_weeks:2", as_of=d.as_of_date, data_start=d.start_date).window
    baseline = resolve_compare(
        resolve("last_n_weeks:2", as_of=d.as_of_date, data_start=d.start_date),
        None,
        as_of=d.as_of_date,
        data_start=d.start_date,
    ).window

    head, _ = compare_periods(
        engine, policy, metric="attempt_success_rate", current=current, baseline=baseline,
        settings=a,
    )  # fmt: skip
    typer.echo(f"Role: {policy.label}.  Current {current.label()} vs baseline {baseline.label()}")
    typer.echo(
        f"Attempt SR {head.rate0:.2%} [{head.ci0[0]:.2%}, {head.ci0[1]:.2%}] -> "
        f"{head.rate1:.2%} [{head.ci1[0]:.2%}, {head.ci1[1]:.2%}]  "
        f"delta {head.delta_pp:+.2f}pp  z={head.z:.1f}  p={head.p_value:.1e}  "
        f"practically significant: {head.practically_significant}"
    )

    ranking, decomps, _ = scan_drivers(
        engine, policy, current=current, baseline=baseline, settings=a
    )
    method = next(x for x in decomps if x.dimension == "payment_method")
    typer.echo(
        f"\nKitagawa by payment_method: mix {method.mix_effect * 100:+.2f}pp, "
        f"rate {method.rate_effect * 100:+.2f}pp, mix share {method.mix_share:.0%}"
    )
    typer.echo(f"  {'segment':<12} {'share0':>7} {'share1':>7} {'rate0':>7} {'rate1':>7} "
               f"{'mix(c)':>8} {'rate':>8}")  # fmt: skip
    for seg in method.segments:
        typer.echo(
            f"  {seg.segment:<12} {seg.share0:>7.1%} {seg.share1:>7.1%} {seg.rate0:>7.1%} "
            f"{seg.rate1:>7.1%} {seg.mix_centered * 100:>+7.2f}pp {seg.rate * 100:>+7.2f}pp"
        )

    typer.echo(f"\nDriver ranking ({ranking.segments_tested} segments tested, BH-corrected):")
    for dim in ranking.dimensions:
        typer.echo(
            f"  {dim.rank}. {dim.dimension:<15} strength {dim.strength:.2f}  {dim.reasons[0]}"
        )

    local, _ = localize_rate_effect(engine, policy, current=current, baseline=baseline, settings=a)
    typer.echo("\nRate-effect sources (within payment method):")
    for seg in local.top_rate_segments[:3]:
        typer.echo(
            f"  {seg.segment:<32} rate {seg.rate0:.1%} -> {seg.rate1:.1%}  "
            f"contribution {seg.rate_pp:+.2f}pp  q={seg.q_value:.1e}"
        )

    if local.top_rate_segments:
        issuer, method_name = local.top_rate_segments[0].segment.split(" | ")
        dim_name = local.top_rate_segments[0].dimension.split(" x ")[0]
        codes, _ = failure_code_shift(
            engine, policy, current=current, baseline=baseline,
            filters=[Filter(dimension=dim_name, op="=", values=[issuer]),
                     Filter(dimension="payment_method", op="=", values=[method_name])],
        )  # fmt: skip
        typer.echo(f"\nFailure-code shift for {issuer} {method_name} (per 100 attempts):")
        for c in codes[:3]:
            typer.echo(f"  {c.failure_code:<22} {c.per100_0:5.2f} -> {c.per100_1:5.2f} "
                       f"({c.delta_per100:+.2f})")  # fmt: skip

    sens, _ = denominator_sensitivity(engine, policy, window=current, settings=a)
    typer.echo("\nDenominator sensitivity (current window):")
    for v in sens.values:
        typer.echo(f"  {v.metric:<24} {v.rate:.2%}  ({v.numerator:,.0f} / {v.denominator:,.0f})")
    typer.echo(f"  max divergence {sens.max_divergence_pp:.2f}pp; note needed: {sens.note_needed}")
    engine.close()


if __name__ == "__main__":
    app()
