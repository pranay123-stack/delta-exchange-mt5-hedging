"""Command line interface.

A thin shell over :class:`HedgeLabService` -- the CLI and the API cannot
disagree about behaviour because they call the same methods.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .backtest.engine import BacktestConfig, Backtester
from .config import Settings, get_settings
from .domain.enums import HedgeObjective, OptimizerMode
from .hedge.optimizer import OptimizerConfig
from .instruments.registry import InstrumentRegistry
from .logging_setup import configure_logging
from .scenarios.runner import ScenarioRunner
from .service import HedgeLabService

console = Console()
app = typer.Typer(
    add_completion=False,
    help="HedgeLab -- cross-platform perpetual/MT5 paper hedging. PAPER TRADING ONLY.",
)
instruments_app = typer.Typer(help="Inspect the instrument catalogue.")
scenario_app = typer.Typer(help="Run the predefined demo scenarios.")
backtest_app = typer.Typer(help="Replay a price path through the hedge engine.")
app.add_typer(instruments_app, name="instruments")
app.add_typer(scenario_app, name="scenario")
app.add_typer(backtest_app, name="backtest")

D = Decimal


def _settings(verbose: bool = False) -> Settings:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else "WARNING", json_output=False)
    return settings


async def _with_service(fn: Any, *, create_schema: bool = True) -> Any:
    settings = get_settings()
    service = HedgeLabService(settings)
    try:
        await service.startup(
            create_schema=create_schema or settings.database_url.startswith("sqlite")
        )
        service.advance_market(20)
        return await fn(service)
    finally:
        await service.shutdown()


def _banner() -> None:
    console.print(Panel.fit(
        "[bold]HedgeLab[/bold]  --  perpetual <-> MT5 hedging\n"
        "[yellow]PAPER TRADING ONLY.[/yellow] No live adapter is registered; "
        "no order can reach a real venue.",
        border_style="yellow",
    ))


# ======================================================================
# instruments
# ======================================================================
@instruments_app.command("list")
def instruments_list(venue: str | None = typer.Option(None, help="Filter by venue")) -> None:
    """Show every configured instrument and how its quantity unit is sized."""
    settings = _settings()
    registry = InstrumentRegistry.from_directory(settings.instrument_spec_dir)
    table = Table(title="Instrument catalogue", show_lines=False)
    for column in ("Key", "Type", "Unit", "Sizing", "Quote", "Settle",
                   "Step", "Min", "Fees bps", "Financing"):
        table.add_column(column, overflow="fold")
    for spec in registry.all():
        if venue and spec.venue != venue:
            continue
        financing = (
            f"funding {spec.baseline_funding_rate}/{spec.funding_interval_hours}h"
            if spec.funding_model.value == "PERPETUAL_FUNDING"
            else f"swap {spec.swap_long_points}/{spec.swap_short_points} pts"
            if spec.funding_model.value == "SWAP_POINTS" else "none"
        )
        table.add_row(
            spec.key, spec.instrument_type.value, spec.quantity_unit.value,
            spec.describe_sizing(), spec.quote_asset, spec.settlement_asset,
            str(spec.quantity_step), str(spec.min_quantity),
            f"{spec.maker_fee_bps}/{spec.taker_fee_bps}", financing,
        )
    console.print(table)


@instruments_app.command("pairs")
def instruments_pairs() -> None:
    """Show the configured hedge pairs and their unit conversions."""
    from .domain.quantity import describe_conversion
    from .marketdata.simulator import MarketSimulator

    settings = _settings()
    registry = InstrumentRegistry.from_directory(settings.instrument_spec_dir)
    simulator = MarketSimulator(registry.all(), seed=settings.simulator_seed)
    simulator.advance(20)

    table = Table(title="Hedge pairs")
    for column in ("Pair", "Objective", "Tolerance", "Conversion", "Enabled"):
        table.add_column(column, overflow="fold")
    for mapping in registry.mappings():
        source = registry.get(mapping.source_key)
        hedge = registry.get(mapping.hedge_key)
        table.add_row(
            mapping.name, mapping.objective.value, f"{mapping.tolerance_bps} bps",
            describe_conversion(
                source, hedge,
                simulator.ticker(source.key).mid, simulator.ticker(hedge.key).mid,
            ),
            "yes" if mapping.enabled else "no",
        )
    console.print(table)


# ======================================================================
# hedge calculation
# ======================================================================
@app.command("calculate")
def calculate(
    source: str = typer.Option(..., help="Source instrument, VENUE:SYMBOL"),
    hedge: str = typer.Option(..., help="Hedge instrument, VENUE:SYMBOL"),
    quantity: str = typer.Option(..., help="Signed source quantity in venue units"),
    objective: str = typer.Option("QUOTE_PNL_NEUTRAL", help="Hedge objective"),
    ratio: str = typer.Option("1", help="Target hedge ratio"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Calculate a hedge and print the full derivation."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await service.calculate_hedge(
            source_key=source, hedge_key=hedge, source_quantity=D(quantity),
            objective=HedgeObjective(objective.upper()), target_ratio=D(ratio),
            use_live_positions=False,
        )

    result = asyncio.run(_with_service(run))
    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        return

    _banner()
    table = Table(title=f"{source} -> {hedge}  ({objective})", show_lines=True)
    table.add_column("Step")
    table.add_column("Formula", overflow="fold")
    table.add_column("Value", overflow="fold")
    for step in result.steps:
        table.add_row(step.label, step.formula, step.value)
    console.print(table)

    summary = Table(title="Result", show_header=False)
    summary.add_column("Field")
    summary.add_column("Value")
    for label, value in (
        ("Required quantity", result.required_quantity),
        ("Rounded quantity", result.rounded_quantity),
        ("Hedge ratio", result.hedge_ratio),
        ("Residual (bps of source)", result.residual_bps),
        ("Execution cost", result.total_execution_cost),
        ("Funding per day", result.funding_impact_per_day),
        ("Margin required", result.margin_requirement),
        ("Expected P&L", result.expected_pnl),
        ("Worst case (99%)", result.worst_case_pnl),
        ("Break-even move %", result.break_even_move_pct),
        ("Executable", result.is_executable),
    ):
        summary.add_row(label, str(value))
    console.print(summary)
    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")


@app.command("optimize")
def optimize(
    source: str = typer.Option(...), hedge: str = typer.Option(...),
    quantity: str = typer.Option(...),
    objective: str = typer.Option("BASE_ASSET_NEUTRAL"),
    mode: str = typer.Option("WEIGHTED", help="EXACT, STEP_SEARCH or WEIGHTED"),
    priority: str = typer.Option("MIN_RESIDUAL"),
    steps: int = typer.Option(5, help="Lattice points to search each side"),
) -> None:
    """Search the quantity lattice and show every candidate considered."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await service.optimize_hedge(
            OptimizerConfig(mode=OptimizerMode(mode.upper()), priority=priority.upper(),
                            search_steps=steps),
            source_key=source, hedge_key=hedge, source_quantity=D(quantity),
            objective=HedgeObjective(objective.upper()), use_live_positions=False,
        )

    result = asyncio.run(_with_service(run))
    table = Table(title=f"Optimiser candidates ({mode}/{priority})")
    for column in ("Quantity", "Residual bps", "Cost", "Funding/day",
                   "Margin", "Slippage", "Capital eff.", "Score"):
        table.add_column(column, justify="right")
    for candidate in result.candidates:
        marker = " <-" if candidate.quantity == result.best.quantity else ""
        table.add_row(
            f"{candidate.quantity}{marker}", f"{candidate.residual_bps:.2f}",
            f"{candidate.execution_cost:.2f}", f"{candidate.funding_per_day:.2f}",
            f"{candidate.margin:.0f}", f"{candidate.slippage:.2f}",
            f"{candidate.capital_efficiency:.2f}", f"{candidate.score:.4f}",
        )
    console.print(table)
    console.print(f"[bold]exact:[/bold] {result.exact_quantity}")
    console.print(f"[bold]chosen:[/bold] {result.best.quantity}")
    console.print(result.rationale)


# ======================================================================
# scenarios
# ======================================================================
@scenario_app.command("list")
def scenario_list() -> None:
    """List the predefined demo scenarios."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return ScenarioRunner(service).available()

    table = Table(title="Demo scenarios")
    table.add_column("Key")
    table.add_column("Name")
    table.add_column("Description")
    for definition in asyncio.run(_with_service(run)):
        table.add_row(definition["key"], definition["name"], definition["description"])
    console.print(table)


@scenario_app.command("run")
def scenario_run(
    key: str = typer.Argument(..., help="Scenario letter A-J, or its name"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run one scenario end to end against the real engine."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await ScenarioRunner(service).run(key)

    result = asyncio.run(_with_service(run))
    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        raise typer.Exit(0 if result.passed else 1)

    _banner()
    console.print(Panel.fit(
        f"[bold]{result.key} -- {result.name}[/bold]",
        border_style="green" if result.passed else "red",
    ))
    for index, step in enumerate(result.steps, 1):
        console.print(f"[bold cyan]{index:2}. {step.label}[/bold cyan]")
        console.print(f"    {step.detail}")
        for field, value in step.data.items():
            if isinstance(value, list | dict):
                value = json.dumps(value, default=str)[:400]
            console.print(f"      [dim]{field}[/dim] = {value}")
    console.print()
    console.print(Panel(result.summary or "(no summary)",
                        title="PASSED" if result.passed else "FAILED",
                        border_style="green" if result.passed else "red"))
    if result.error:
        console.print(f"[red]error:[/red] {result.error}")
    raise typer.Exit(0 if result.passed else 1)


@scenario_app.command("run-all")
def scenario_run_all() -> None:
    """Run every scenario and report a pass/fail table."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await ScenarioRunner(service).run_all()

    results = asyncio.run(_with_service(run))
    table = Table(title="Scenario results")
    table.add_column("Key")
    table.add_column("Name")
    table.add_column("Result")
    table.add_column("Summary", overflow="fold")
    for result in results:
        table.add_row(
            result.key, result.name,
            "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]",
            result.summary or result.error or "",
        )
    console.print(table)
    failures = [r for r in results if not r.passed]
    console.print(f"{len(results) - len(failures)}/{len(results)} scenarios passed")
    raise typer.Exit(1 if failures else 0)


# ======================================================================
# backtest
# ======================================================================
@backtest_app.command("run")
def backtest_run(
    source: str = typer.Option("PAPER_DELTA:BTCUSDT-PERP"),
    hedge: str = typer.Option("PAPER_MT5:BTCUSD"),
    quantity: str = typer.Option("5000"),
    objective: str = typer.Option("QUOTE_PNL_NEUTRAL"),
    ratio: str = typer.Option("1"),
    steps: int = typer.Option(500),
    step_seconds: str = typer.Option("3600", help="Wall-clock seconds per step"),
    tolerance_bps: str = typer.Option("25"),
    seed: int = typer.Option(20260908),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Replay a price path through the real hedge maths."""
    settings = _settings()
    registry = InstrumentRegistry.from_directory(settings.instrument_spec_dir)
    report = Backtester(registry.all()).run(BacktestConfig(
        source_key=source, hedge_key=hedge, source_quantity=D(quantity),
        objective=HedgeObjective(objective.upper()), target_ratio=D(ratio),
        steps=steps, step_seconds=D(step_seconds),
        tolerance_bps=D(tolerance_bps), seed=seed,
    ))
    if as_json:
        console.print_json(json.dumps(report.to_dict()))
        return

    table = Table(title="Backtest result", show_header=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for label, value in (
        ("Simulated days", D(step_seconds) * D(steps) / D(86400)),
        ("Rebalances", report.rebalances),
        ("Net P&L", report.final_net_pnl),
        ("  funding / swap", report.total_funding),
        ("  fees", -report.total_fees),
        ("  slippage", -report.total_slippage),
        ("Residual mean (bps)", report.mean_residual_bps),
        ("Residual max (bps)", report.max_residual_bps),
        ("Price drawdown (hedged)", abs(report.gross_drawdown)),
        ("Price drawdown (unhedged)", report.unhedged_max_drawdown),
        ("Drawdown removed", f"{report.risk_reduction_pct:.1f}%"),
        ("Peak margin", report.peak_margin),
    ):
        table.add_row(label, str(value))
    console.print(table)
    console.print(
        "[dim]Risk reduction compares price P&L only. Carry and execution cost are "
        "reported separately and are usually negative: hedging costs money.[/dim]"
    )


# ======================================================================
# operational
# ======================================================================
@app.command("status")
def status() -> None:
    """Show system status."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await service.status()

    payload = asyncio.run(_with_service(run))
    _banner()
    console.print_json(json.dumps(payload, default=str))


@app.command("demo")
def demo() -> None:
    """Run the full 25-step acceptance demonstration."""
    from .scenarios.acceptance import run_acceptance

    _settings()
    passed = asyncio.run(_with_service(run_acceptance))
    raise typer.Exit(0 if passed else 1)


@app.command("serve")
def serve(
    host: str | None = typer.Option(None), port: int | None = typer.Option(None),
    reload: bool = typer.Option(False, help="Auto-reload on source changes"),
) -> None:
    """Start the API server."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "hedgelab.api.app:app",
        host=host or settings.api_host, port=port or settings.api_port,
        reload=reload, log_config=None,
    )


@app.command("init-db")
def init_db(drop: bool = typer.Option(False, help="Drop existing tables first")) -> None:
    """Create the database schema and load the instrument catalogue."""
    settings = _settings()

    async def run() -> None:
        service = HedgeLabService(settings)
        try:
            if drop:
                await service.database.drop_all()
            await service.database.create_all()
            await service.sync_reference_data()
            console.print(
                f"[green]schema ready[/green]: {len(service.specs)} instruments, "
                f"{len(service.registry.mappings())} hedge pairs"
            )
        finally:
            await service.shutdown()

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    app()
