# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Forven CLI — main entry point."""

import click

from forven.config import ensure_dirs


@click.group()
@click.version_option(package_name="forven")
def cli():
    """Forven — Algorithmic trading operations framework."""
    ensure_dirs()


# --- Auth commands ---

@cli.group()
def auth():
    """Manage authentication for AI providers."""


@auth.command("status")
def auth_status():
    """Show all provider auth profiles and token expiry."""
    from forven.auth.store import display_status
    display_status()


@auth.command("login")
@click.argument("provider", type=click.Choice(["openai", "minimax"]))
def auth_login(provider):
    """Authenticate with a specific AI provider."""
    from forven.auth.store import run_login
    run_login(provider)


@auth.command("refresh")
@click.argument("provider", type=click.Choice(["openai", "minimax"]))
def auth_refresh(provider):
    """Force-refresh tokens for a provider."""
    from forven.auth.store import force_refresh
    force_refresh(provider)


@auth.command("migrate")
def auth_migrate():
    """Import tokens from OpenClaw."""
    from forven.auth.store import migrate_from_openclaw
    migrate_from_openclaw()


# --- Configure ---

@cli.command()
def configure():
    """Interactive setup — select providers and run OAuth flows."""
    from forven.auth.store import interactive_configure
    interactive_configure()


# --- AI commands ---

@cli.group()
def ai():
    """AI provider operations."""


@ai.command("ask")
@click.argument("provider", type=click.Choice(["openai", "minimax", "lmstudio"]))
@click.argument("prompt")
@click.option("--model", "-m", default=None, help="Model ID override")
def ai_ask(provider, prompt, model):
    """Quick one-shot AI call for testing."""
    from forven.ai import call_ai_sync
    from forven.model_selection import ensure_enforcement_armed
    ensure_enforcement_armed()
    result = call_ai_sync(provider=provider, prompt=prompt, model=model)
    click.echo(result)


# --- Database commands ---

@cli.group()
def db():
    """Database operations."""


@db.command("init")
def db_init():
    """Initialize the SQLite database."""
    from forven.db import init_db
    init_db()
    click.echo("Database initialized.")


@db.command("migrate")
def db_migrate():
    """Import existing JSON state from OpenClaw into SQLite."""
    from forven.config import OPENCLAW_WORKSPACE
    from forven.db import init_db, migrate_from_openclaw

    init_db()
    data_dir = OPENCLAW_WORKSPACE / "trading" / "data"
    if not data_dir.exists():
        click.echo(f"Data directory not found: {data_dir}")
        return
    migrate_from_openclaw(data_dir)


@db.command("restore-snapshot")
@click.argument("snapshot_id")
def db_restore_snapshot(snapshot_id):
    """Restore a strategy's stage/status from a migration snapshot."""
    from forven.db import init_db, restore_migration_snapshot

    init_db()
    try:
        result = restore_migration_snapshot(snapshot_id)
        click.echo(f"Restored {result['strategy_id']} to stage={result['restored_stage']}")
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@db.command("status")
def db_status():
    """Show table row counts."""
    from rich.console import Console
    from rich.table import Table
    from forven.db import init_db, table_counts

    init_db()
    counts = table_counts()
    console = Console()
    table = Table(title="Database Status")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    for name, count in counts.items():
        table.add_row(name, str(count))
    console.print(table)


# --- Workspace commands ---

@cli.group()
def workspace():
    """Workspace identity files."""


@workspace.command("init")
@click.option("--force", is_flag=True, help="Overwrite existing files")
def workspace_init(force):
    """Initialize workspace from OpenClaw or defaults."""
    from forven.workspace import init_workspace
    init_workspace(force=force)


@workspace.command("list")
def workspace_list():
    """List all workspace files."""
    from forven.workspace import list_workspace_files
    for f in list_workspace_files():
        click.echo(f"  {f}")


@workspace.command("read")
@click.argument("filename")
def workspace_read(filename):
    """Display a workspace file."""
    from forven.workspace import read_workspace
    content = read_workspace(filename)
    click.echo(content)


# --- Trades commands ---

@cli.group()
def trades():
    """Trade management."""


@trades.command("list")
@click.option("--limit", "-l", default=20, help="Max results")
def trades_list(limit):
    """Show recent trades."""
    from rich.console import Console
    from rich.table import Table
    from forven.db import init_db, get_recent_trades

    init_db()
    results = get_recent_trades(limit)
    if not results:
        click.echo("No trades found.")
        return

    console = Console()
    table = Table(title="Recent Trades")
    table.add_column("ID", max_width=12)
    table.add_column("Asset")
    table.add_column("Dir")
    table.add_column("Entry")
    table.add_column("PnL %")
    table.add_column("Status")
    table.add_column("Strategy")

    for t in results:
        pnl = f"{t['pnl_pct']:+.2f}%" if t.get("pnl_pct") is not None else "-"
        table.add_row(
            str(t["id"])[:12], t.get("asset", ""), t.get("direction", ""),
            str(t.get("entry_price", "")), pnl, t.get("status", ""), t.get("strategy", ""),
        )
    console.print(table)


@trades.command("open")
def trades_open():
    """Show open positions."""
    from rich.console import Console
    from rich.table import Table
    from forven.db import init_db, get_open_trades

    init_db()
    results = get_open_trades()
    if not results:
        click.echo("No open positions.")
        return

    console = Console()
    table = Table(title="Open Positions")
    table.add_column("Asset")
    table.add_column("Direction")
    table.add_column("Entry Price")
    table.add_column("Strategy")
    table.add_column("Opened")

    for t in results:
        table.add_row(
            t.get("asset", ""), t.get("direction", ""),
            str(t.get("entry_price", "")), t.get("strategy", ""),
            t.get("opened_at", ""),
        )
    console.print(table)


# --- Strategy commands ---

@cli.group()
def strategies():
    """Strategy registry."""


@strategies.command("list")
@click.option("--status", "-s", default=None, help="Filter by status")
def strategies_list(status):
    """Show strategy registry."""
    from rich.console import Console
    from rich.table import Table
    from forven.db import init_db, get_strategies

    init_db()
    results = get_strategies(status)
    if not results:
        click.echo("No strategies found.")
        return

    console = Console()
    table = Table(title="Strategy Registry")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Symbol")
    table.add_column("Timeframe")

    for s in results:
        table.add_row(
            s["id"], s.get("name", ""), s.get("status", ""),
            s.get("symbol", ""), s.get("timeframe", ""),
        )
    console.print(table)


@strategies.command("triage-stale")
@click.option("--days", "-d", default=7, show_default=True, type=int,
              help="Quick-screen strategies older than N days with no recent agent_task activity will be archived.")
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Actually archive. Without this flag the command runs as dry-run.")
def strategies_triage_stale(days: int, do_apply: bool) -> None:
    """Bulk-archive stale quick_screen strategies that never advanced."""
    import datetime as _dt
    from forven.db import get_db
    from forven.brain import transition_stage

    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, stage_changed_at
            FROM strategies
            WHERE LOWER(TRIM(stage)) = 'quick_screen'
              AND stage_changed_at IS NOT NULL
              AND stage_changed_at < ?
              AND id NOT IN (
                  SELECT strategy_id FROM agent_tasks
                  WHERE strategy_id IS NOT NULL AND created_at >= ?
              )
            ORDER BY stage_changed_at ASC
            """,
            (cutoff, cutoff),
        ).fetchall()

    candidates = [dict(row) for row in rows]
    mode = "APPLY" if do_apply else "DRY-RUN"
    click.echo(f"[{mode}] Found {len(candidates)} stale quick_screen strategies (>{days}d, no recent activity).")
    for c in candidates:
        click.echo(f"  - {c['id']}  {c.get('name','')}  stage_changed_at={c['stage_changed_at']}")

    if not do_apply:
        click.echo("Dry-run only. Re-run with --apply to archive.")
        return

    archived = 0
    failed = 0
    reason = f"stale: no activity in {days}d"
    for c in candidates:
        try:
            # actor="triage-cli" must be in brain._USER_ACTORS for force=True to
            # bypass verify_fitness_before_archive (stale quick_screen strategies
            # legitimately have no metrics and would otherwise be blocked).
            transition_stage(c["id"], "archived", reason=reason, actor="triage-cli", force=True)
            archived += 1
        except Exception as exc:
            failed += 1
            click.echo(f"  ! failed to archive {c['id']}: {exc}", err=True)

    click.echo(f"Done. archived={archived} failed={failed}")


@strategies.command("triage-orphans")
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Actually archive orphans as untestable. Without this flag the command runs as dry-run.")
def strategies_triage_orphans(do_apply: bool) -> None:
    """List strategies whose runtime type has no registered class or param family.

    Orphans silently fail optimization, chart overlays, promotion gates, and
    paper/live execution. They typically come from LLM-fabricated type names
    that never had a backing class file.
    """
    from forven.db import get_db
    from forven.brain import archive_untestable
    from forven.strategies.params import is_known_runtime_type

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, type, stage FROM strategies
            WHERE stage NOT IN ('archived', 'rejected', 'backtest_failed')
            ORDER BY type, id
            """,
        ).fetchall()

    orphans: list[dict] = []
    for row in rows:
        stype = str(row["type"] or "").strip()
        if not stype:
            continue
        if is_known_runtime_type(stype):
            continue
        orphans.append(dict(row))

    mode = "APPLY" if do_apply else "DRY-RUN"
    click.echo(f"[{mode}] Found {len(orphans)} orphan strategies (unregistered runtime type).")
    by_type: dict[str, int] = {}
    for o in orphans:
        by_type[o["type"]] = by_type.get(o["type"], 0) + 1
    for type_name, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
        click.echo(f"  type={type_name}  count={count}")
    for o in orphans:
        click.echo(f"  - {o['id']}  type={o['type']}  stage={o['stage']}  name={o.get('name','')}")

    if not do_apply:
        click.echo("Dry-run only. Re-run with --apply to archive them as untestable.")
        return

    demoted = 0
    failed = 0
    for o in orphans:
        try:
            archive_untestable(
                o["id"],
                code="broken_code",
                detail=(
                    f"orphan runtime type '{o['type']}': "
                    "no registered class and not a known param family"
                ),
                actor="triage-cli",
                force=True,
            )
            demoted += 1
        except Exception as exc:
            failed += 1
            click.echo(f"  ! failed to archive {o['id']}: {exc}", err=True)

    click.echo(f"Done. archived={demoted} failed={failed}")


# --- Bot commands ---

@cli.group()
def bot():
    """Discord bot operations."""


@bot.command("start")
def bot_start():
    """Start the Discord bot (foreground)."""
    from forven.bot import run_bot
    run_bot()


@bot.command("status")
def bot_status():
    """Show bot singleton guard/instance status."""
    from forven.bot import get_bot_lock_status

    status = get_bot_lock_status()
    click.echo(f"Singleton supported: {status.get('singleton_supported', False)}")
    click.echo(f"Lock held: {status.get('lock_held', False)}")
    click.echo(f"Current PID: {status.get('current_pid', '-')}")
    click.echo(f"Active PID: {status.get('active_pid') or '-'}")
    if status.get("stale_pid"):
        click.echo(f"Stale lock-file PID: {status['stale_pid']}")
    if status.get("other_process_active"):
        click.echo("Guard: another process currently holds the bot lock.")
    elif status.get("held_by_current_process"):
        click.echo("Guard: this process currently holds the bot lock.")
    else:
        click.echo("Guard: no active bot lock holder.")


@bot.command("send")
@click.argument("channel")
@click.argument("message")
def bot_send(channel, message):
    """Send a message to a Discord channel."""
    import asyncio
    from forven.bot import send, get_bot, get_bot_token

    async def _send():
        b = get_bot()
        token = get_bot_token()
        asyncio.ensure_future(b.start(token))
        await b.wait_until_ready_custom(timeout=15)
        await send(channel, message)
        await b.close()

    asyncio.run(_send())
    click.echo(f"Sent to #{channel}")


# --- Scheduler commands ---

@cli.group()
def scheduler():
    """Job scheduler operations."""


@scheduler.command("list")
def scheduler_list():
    """Show all scheduler jobs."""
    from rich.console import Console
    from rich.table import Table
    from forven.db import init_db
    from forven.scheduler import get_jobs

    init_db()
    jobs = get_jobs()
    if not jobs:
        click.echo("No scheduler jobs. Run `forven scheduler migrate` to import from OpenClaw.")
        return

    console = Console()
    table = Table(title="Scheduler Jobs")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Schedule")
    table.add_column("Enabled")
    table.add_column("Last Status")
    table.add_column("Next Run")

    for j in jobs:
        table.add_row(
            j["name"], j["schedule_type"], j["schedule_expr"],
            "Yes" if j["enabled"] else "No",
            j.get("last_status", "-"),
            (j.get("next_run_at", "-") or "-")[:19],
        )
    console.print(table)


@scheduler.command("migrate")
def scheduler_migrate():
    """Import jobs from OpenClaw."""
    from forven.scheduler import migrate_from_openclaw
    migrate_from_openclaw()


@scheduler.command("enable")
@click.argument("job_id")
def scheduler_enable(job_id):
    """Enable a scheduler job."""
    from forven.scheduler import enable_job
    enable_job(job_id, True)
    click.echo(f"Enabled job: {job_id}")


@scheduler.command("disable")
@click.argument("job_id")
def scheduler_disable(job_id):
    """Disable a scheduler job."""
    from forven.scheduler import enable_job
    enable_job(job_id, False)
    click.echo(f"Disabled job: {job_id}")


@scheduler.command("run")
@click.argument("job_id")
def scheduler_run(job_id):
    """Force-run a scheduler job now."""
    import asyncio
    from forven.db import init_db
    from forven.scheduler import run_job

    init_db()

    with __import__("forven.db", fromlist=["get_db"]).get_db() as conn:
        row = conn.execute("SELECT * FROM scheduler_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            click.echo(f"Job not found: {job_id}")
            return

    status, error = asyncio.run(run_job(dict(row)))
    if error:
        click.echo(f"Job failed: {error}")
    else:
        click.echo(f"Job completed: {status}")


# --- Scanner commands ---

@cli.group()
def scanner():
    """Multi-strategy scanner (all 10 strategies)."""


@scanner.command("run")
def scanner_run():
    """Run a single multi-strategy scan (all 10 strategies)."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    from forven.scanner import run_scan
    signals = run_scan()
    click.echo(f"\nScanned {len(signals)} strategies.")


@scanner.command("status")
def scanner_status():
    """Show last scanner state."""
    from forven.db import init_db, kv_get
    init_db()
    state = kv_get("scanner_state", {})
    if not state:
        click.echo("No scanner state found. Run `forven scanner run` first.")
        return
    click.echo(f"Last scan: {state.get('last_scan', '?')}")
    click.echo(f"Strategies: {len(state.get('strategies', []))}")
    click.echo(f"Open: {state.get('open_positions', 0)} | Closed: {state.get('closed_trades', 0)}")
    click.echo(f"Total PnL: {state.get('total_pnl_pct', 0):+.2%}")


@scanner.command("strategies")
def scanner_strategies():
    """List all scanner strategy definitions."""
    from rich.console import Console
    from rich.table import Table
    from forven.scanner import STRATEGIES

    console = Console()
    table = Table(title="Scanner Strategies (10)")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Asset")
    table.add_column("Type")
    table.add_column("Fitness v2")

    for sid, s in STRATEGIES.items():
        fv2 = str(s.get("fitness_v2", "-"))
        table.add_row(sid, s["name"], s["asset"], s["type"], fv2)
    console.print(table)


@scanner.command("backtest")
@click.option("--bars", "-b", default=None, type=int, help="Number of hourly bars (reads from settings if omitted)")
@click.option("--strategy", "-s", default=None, help="Specific strategy ID (default: all)")
def scanner_backtest(bars, strategy):
    """Run backtests on all strategies and compute fitness scores."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    from rich.console import Console
    from rich.table import Table
    from forven.strategies.backtest import backtest_strategy, backtest_all, save_backtest_results
    from forven.strategies.fitness import evaluate_all
    from forven.scanner import STRATEGIES

    console = Console()

    if strategy:
        if strategy not in STRATEGIES:
            console.print(f"[red]Strategy not found: {strategy}[/red]")
            return
        s = STRATEGIES[strategy]
        result = backtest_strategy(strategy, s["asset"], s["type"], s["params"], bars, s["params"].get("leverage", 3.0), regime_gate=False)
        results = {strategy: result}
    else:
        console.print(f"[bold]Backtesting all strategies ({bars} bars = {bars // 24} days)...[/bold]\n")
        results = backtest_all(bars)

    save_backtest_results(results)
    fitness_results = evaluate_all()

    table = Table(title="Backtest Results + Fitness Scores")
    table.add_column("Strategy")
    table.add_column("Trades", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Sharpe", justify="right")
    table.add_column("Max DD", justify="right")
    table.add_column("PF", justify="right")
    table.add_column("Return", justify="right")
    table.add_column("Fitness", justify="right")
    table.add_column("Verdict")

    for fr in fitness_results:
        m = fr.get("metrics") or {}
        if not m.get("total_trades"):
            continue
        style = "green" if fr["fitness"] >= 70 else ("yellow" if fr["fitness"] >= 60 else "red")
        table.add_row(
            fr["id"],
            str(m.get("total_trades", 0)),
            f"{(m.get('win_rate') or 0):.0%}",
            f"{(m.get('sharpe') or 0):.2f}",
            f"{(m.get('max_drawdown_pct') or 0):.1%}",
            f"{(m.get('profit_factor') or 0):.2f}",
            f"{(m.get('total_return_pct') or 0):.1%}",
            f"[{style}]{fr['fitness']:.1f}[/{style}]",
            fr["verdict"],
        )
    console.print(table)
    if not any((fr.get("metrics") or {}).get("total_trades") for fr in fitness_results):
        console.print("[dim]No backtest results with trades yet.[/dim]")


@scanner.command("walkforward")
@click.option("--bars", default=1440, help="Total hourly bars (default 1440 = 60 days)")
@click.option("--splits", default=3, help="Number of walk-forward splits")
@click.option("--strategy", default=None, help="Run single strategy (ID)")
def scanner_walkforward(bars, splits, strategy):
    """Run walk-forward analysis on strategies (out-of-sample validation)."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    from rich.console import Console
    from rich.table import Table
    from forven.strategies.backtest import walk_forward
    from forven.scanner import STRATEGIES

    console = Console()

    targets = {strategy: STRATEGIES[strategy]} if strategy else STRATEGIES
    if strategy and strategy not in STRATEGIES:
        console.print(f"[red]Strategy not found: {strategy}[/red]")
        return

    table = Table(title=f"Walk-Forward Analysis ({splits} splits, {bars} bars)")
    table.add_column("Strategy")
    table.add_column("IS Sharpe", justify="right")
    table.add_column("OOS Sharpe", justify="right")
    table.add_column("Degradation", justify="right")
    table.add_column("OOS Trades", justify="right")
    table.add_column("OOS Return", justify="right")
    table.add_column("Verdict")

    for sid, s in targets.items():
        if s["type"] == "funding":
            continue
        console.print(f"[dim]Testing {sid}...[/dim]")
        result = walk_forward(
            sid, s["asset"], s["type"], s["params"],
            total_bars=bars, n_splits=splits,
            leverage=s["params"].get("leverage", 3.0),
        )
        if result.get("error"):
            table.add_row(sid, "—", "—", "—", "—", "—", f"[red]{result['error'][:30]}[/red]")
            continue
        oos = result.get("aggregate_oos", {})
        style = "green" if result["verdict"] == "PASS" else "red"
        table.add_row(
            sid,
            f"{result['avg_is_sharpe']:.2f}",
            f"{result['avg_oos_sharpe']:.2f}",
            f"{result['degradation']:.0%}",
            str(oos.get("total_trades", 0)),
            f"{oos.get('total_return_pct', 0):.1%}",
            f"[{style}]{result['verdict']}[/{style}]",
        )
        import time
        time.sleep(0.5)

    console.print(table)


# --- Daemon commands ---

@cli.group()
def daemon():
    """Trading daemon operations."""


@daemon.command("start")
def daemon_start():
    """Start the trading daemon (foreground)."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    from forven.daemon import run
    run()


@daemon.command("status")
def daemon_status():
    """Show daemon state."""
    from forven.db import init_db
    from forven.runtime_health import normalize_daemon_state

    init_db()
    state = normalize_daemon_state(write_back=True)
    if not state:
        click.echo("No daemon state found. Daemon may not have run yet.")
        return
    click.echo(f"Running: {state.get('running', False)}")
    click.echo(f"Started: {state.get('started_at', '?')}")
    click.echo(f"Scans: {state.get('scan_count', 0)}")
    click.echo(f"Last scan: {state.get('last_scan', '?')}")
    prices = state.get("last_prices", {})
    if prices:
        click.echo("Prices: " + " | ".join(f"{k}=${v:,.2f}" for k, v in prices.items()))


# --- Brain commands ---

@cli.group()
def brain():
    """Brain orchestrator operations."""


@brain.command("invoke")
@click.option("--provider", "-p", default=None, help="AI provider override")
@click.option("--model", "-m", default=None, help="Model override")
def brain_invoke(provider, model):
    """Manually trigger the brain cycle."""
    from forven.brain import invoke_sync
    from forven.model_selection import ensure_enforcement_armed
    ensure_enforcement_armed()
    result = invoke_sync(provider=provider, model=model)
    click.echo(result)


@brain.command("ask")
@click.argument("question")
@click.option("--provider", "-p", default=None, help="AI provider override")
def brain_ask(question, provider):
    """Ask the brain a question (uses full context)."""
    from forven.brain import invoke_sync
    from forven.model_selection import ensure_enforcement_armed
    ensure_enforcement_armed()
    result = invoke_sync(message=question, provider=provider)
    click.echo(result)


@brain.command("tasks")
def brain_tasks():
    """Show pending brain tasks."""
    from rich.console import Console
    from rich.table import Table
    from forven.db import init_db, get_db

    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE type='brain_invoke' ORDER BY created_at DESC LIMIT 20"
        ).fetchall()

    if not rows:
        click.echo("No brain tasks.")
        return

    console = Console()
    table = Table(title="Brain Tasks")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Created")

    for r in rows:
        table.add_row(str(r["id"]), r["status"], str(r["priority"]), r["created_at"][:19])
    console.print(table)


# --- Risk commands ---

@cli.group()
def risk():
    """Portfolio risk management."""


@risk.command("status")
def risk_status():
    """Show portfolio risk summary with kill-switch and daily loss status."""
    from forven.db import init_db
    from forven.exchange.risk import get_portfolio_summary, get_risk_status, PORTFOLIO_BUDGET

    init_db()
    rs = get_risk_status()
    summary = get_portfolio_summary()

    click.echo(f"\nPortfolio Risk Summary (Budget: {PORTFOLIO_BUDGET:.0%})")
    click.echo("=" * 50)

    # Kill-switch and daily halt status
    if rs["kill_switch_active"]:
        click.echo(f"\n  [KILL SWITCH ACTIVE] Triggered at {rs.get('kill_switch_triggered_at', '?')}")
        click.echo("  All trading halted. Run `forven risk reset` to clear after review.")
    elif rs["daily_loss_halt"]:
        click.echo(f"\n  [DAILY HALT] No new positions until tomorrow ({rs.get('daily_date', '?')})")
    else:
        click.echo("\n  Trading: ACTIVE")

    hwm = rs.get("high_water_mark", 0)
    start_eq = rs.get("daily_start_equity", 0)
    if hwm > 0:
        click.echo(f"  HWM: ${hwm:,.2f} | Daily start: ${start_eq:,.2f}")

    limits = rs["limits"]
    click.echo(f"  Limits: DD {limits['max_drawdown']:.0%} | Daily {limits['daily_loss_limit']:.0%} | Per-trade {limits['max_risk_per_trade']:.0%}")

    for group, data in summary["groups"].items():
        if not data["positions"] and data["net"] == 0:
            continue
        click.echo(f"\n  [{group}]")
        click.echo(f"  Net: {data['net']:+.1%} | Long: {data['gross_long']:.1%} | Short: {data['gross_short']:.1%}")
        for pos in data["positions"]:
            click.echo(f"    {pos['direction'].upper():5} {pos['asset']:6} {pos['risk_pct']:.1%} [{pos['strategy']}]")

    if summary["total_net_risk"] == 0:
        click.echo("\n  No open positions. Full budget available.")


@risk.command("reset")
def risk_reset():
    """Manually reset the kill-switch after review."""
    from forven.db import init_db
    from forven.exchange.risk import reset_kill_switch, get_risk_status

    init_db()
    rs = get_risk_status()
    if not rs["kill_switch_active"]:
        click.echo("Kill-switch is not active.")
        return

    if not click.confirm("Reset the kill-switch? This re-enables trading."):
        return

    reset_kill_switch()
    click.echo("Kill-switch reset. Trading re-enabled.")


# --- Team / Agent commands ---

@cli.group()
def team():
    """Agent team management."""


@team.command("list")
def team_list():
    """Show all agents with stats."""
    from rich.console import Console
    from rich.table import Table
    from forven.db import init_db
    from forven.agents.manager import list_agents_with_stats

    init_db()
    agents = list_agents_with_stats()
    if not agents:
        click.echo("No agents configured. Create one with: forven team create")
        return

    console = Console()
    table = Table(title="Agent Team")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Model")
    table.add_column("Schedule")
    table.add_column("Enabled")
    table.add_column("Pending")
    table.add_column("Done")

    for a in agents:
        table.add_row(
            a["id"], a["name"], a.get("model", ""),
            a.get("schedule_expr", "-"), "Yes" if a.get("enabled") else "No",
            str(a.get("pending_tasks", 0)), str(a.get("completed_tasks", 0)),
        )
    console.print(table)


@team.command("create")
@click.argument("agent_id")
@click.argument("name")
@click.option("--role", "-r", required=True, help="One-line role description")
@click.option("--model", "-m", default="openai", help="AI provider")
@click.option("--model-id", default=None, help="Specific model ID")
@click.option("--schedule", default=None, help="Schedule (cron expr or interval in ms)")
@click.option("--schedule-type", default="on_demand", type=click.Choice(["cron", "interval", "on_demand"]))
def team_create(agent_id, name, role, model, model_id, schedule, schedule_type):
    """Create a new agent."""
    from forven.agents.manager import create_agent
    result = create_agent(agent_id, name, role, model, model_id, schedule_type, schedule)
    click.echo(f"Created agent: {result['name']} ({result['id']})")


@team.command("inspect")
@click.argument("agent_id")
def team_inspect(agent_id):
    """Show detailed agent info."""
    from forven.agents.manager import inspect_agent
    info = inspect_agent(agent_id)
    if "error" in info:
        click.echo(info["error"])
        return
    click.echo(f"Name: {info['name']}")
    click.echo(f"Role: {info['role']}")
    click.echo(f"Model: {info.get('model', '?')}/{info.get('model_id', '?')}")
    click.echo(f"Enabled: {bool(info.get('enabled'))}")
    click.echo(f"Pending tasks: {info.get('pending_tasks', 0)}")
    click.echo(f"Has ROLE.md: {info.get('has_role_md', False)}")
    if info.get("recent_tasks"):
        click.echo("\nRecent tasks:")
        for t in info["recent_tasks"]:
            click.echo(f"  [{t['status']}] {t.get('title', '?')} ({t.get('created_at', '')[:19]})")


@team.command("assign")
@click.argument("agent_id")
@click.argument("task_description")
@click.option("--type", "task_type", default="research", help="Task type")
@click.option("--title", default=None, help="Task title")
def team_assign(agent_id, task_description, task_type, title):
    """Queue a task for an agent (Brain assigns)."""
    from forven.brain import assign_task
    title = title or task_description[:60]
    assign_task(agent_id, task_type, title, task_description)
    click.echo(f"Assigned task to {agent_id}: {title}")


@team.command("enable")
@click.argument("agent_id")
def team_enable(agent_id):
    """Enable an agent."""
    from forven.agents.manager import update_agent
    update_agent(agent_id, enabled=1)
    click.echo(f"Enabled agent: {agent_id}")


@team.command("disable")
@click.argument("agent_id")
def team_disable(agent_id):
    """Disable an agent."""
    from forven.agents.manager import update_agent
    update_agent(agent_id, enabled=0)
    click.echo(f"Disabled agent: {agent_id}")


# --- Evolution ---

@cli.group()
def evolution():
    """Strategy evolution pipeline management."""
    pass


@evolution.command("status")
def evolution_status():
    """Show evolution pipeline status."""
    from rich.console import Console
    from rich.table import Table
    from forven.evolution import get_evolution_status
    from forven.db import init_db

    init_db()
    console = Console()
    status = get_evolution_status()

    console.print("\n[bold]Evolution Pipeline[/bold]\n")

    table = Table()
    table.add_column("Stage", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Strategies")

    for stage in ["researching", "backtesting", "paper", "deployed", "retired", "rejected"]:
        ids = status["strategies_by_status"].get(stage, [])
        color = {"deployed": "green", "paper": "yellow", "retired": "red", "rejected": "red"}.get(stage, "white")
        table.add_row(
            f"[{color}]{stage}[/{color}]",
            str(len(ids)),
            ", ".join(ids[:5]) + ("..." if len(ids) > 5 else ""),
        )

    console.print(table)
    console.print(f"\n[dim]Total strategies: {status['total']}[/dim]")


@evolution.command("ideate")
def evolution_ideate():
    """Queue a strategy-creation task for strategy-developer (within today's budget)."""
    from forven.db import init_db
    from forven.strategy_creation import run_creation_cycle
    init_db()
    result = run_creation_cycle()
    if result.get("status") == "queued":
        click.echo(f"Queued strategy-creation task {result.get('task_id')}")
    else:
        click.echo(f"No task queued: {result.get('reason')}")


@evolution.command("test")
def evolution_test():
    """Trigger testing step (assign WFA to simulation-agent)."""
    from forven.evolution import run_testing_step
    from forven.db import init_db
    init_db()
    run_testing_step()
    click.echo("Testing cycle complete")


@evolution.command("review")
def evolution_review():
    """Trigger weekly review (retire underperformers, assign post-mortems)."""
    from rich.console import Console
    from forven.evolution import run_weekly_review
    from forven.db import init_db

    init_db()
    console = Console()
    result = run_weekly_review()
    if result:
        console.print("[bold]Weekly Review Complete[/bold]")
        if result.get("retired"):
            console.print(f"[red]Retired:[/red] {', '.join(result['retired'])}")
        console.print(f"[green]Top performers:[/green] {', '.join(result.get('top_performers', []))}")
    else:
        console.print("[dim]No deployed strategies to review[/dim]")


@evolution.command("optimize")
@click.argument("strategy_id", required=False)
def evolution_optimize(strategy_id):
    """Optimize strategy parameters (grid search + WFA)."""
    from rich.console import Console
    from forven.strategies.optimizer import optimize_strategy, optimize_all_deployed
    from forven.db import init_db

    init_db()
    console = Console()

    if strategy_id:
        console.print(f"Optimizing {strategy_id}...")
        result = optimize_strategy(strategy_id)
        if result.get("error"):
            console.print(f"[red]Error: {result['error']}[/red]")
        else:
            console.print(f"[bold]Best params:[/bold] {result['best_params']}")
            console.print(f"[bold]Fitness:[/bold] {result['best_fitness']:.1f}")
            console.print(f"[bold]WFA:[/bold] {result['wfa_verdict']}")
            console.print(f"[bold]Validated:[/bold] {'Yes' if result['validated'] else 'No'}")
    else:
        console.print("Optimizing all deployed strategies...")
        results = optimize_all_deployed()
        for r in results:
            if r.get("error"):
                console.print(f"[red]{r['strategy_id']}: {r['error']}[/red]")
            else:
                console.print(f"[green]{r['strategy_id']}:[/green] fitness={r['best_fitness']:.1f} WFA={r['wfa_verdict']}")


# --- Regime ---

@cli.group()
def regime():
    """Market regime detection."""
    pass


@regime.command("status")
def regime_status():
    """Show current market regimes for all tracked assets."""
    from rich.console import Console
    from rich.table import Table
    from forven.regime import detect_all_regimes
    from forven.db import init_db

    init_db()
    console = Console()
    console.print("\n[bold]Market Regimes[/bold]\n")

    regimes = detect_all_regimes()

    table = Table()
    table.add_column("Asset", style="bold")
    table.add_column("Regime")
    table.add_column("Confidence", justify="right")
    table.add_column("ADX", justify="right")
    table.add_column("EMA Alignment")
    table.add_column("ATR Ratio", justify="right")
    table.add_column("RSI", justify="right")

    for asset, state in regimes.items():
        color = {
            "TREND_UP": "green", "TREND_DOWN": "red",
            "RANGE_BOUND": "yellow", "HIGH_VOL": "magenta",
        }.get(state.regime, "white")

        table.add_row(
            asset,
            f"[{color}]{state.regime}[/{color}]",
            f"{state.confidence:.0%}",
            f"{state.adx:.1f}",
            state.ema_alignment,
            f"{state.atr_ratio:.2f}",
            f"{state.rsi:.1f}",
        )

    console.print(table)


# --- Regime Lab ---

@cli.group()
def lab():
    """Regime Lab worker and diagnostics."""
    pass


@lab.command("worker")
@click.option("--once", is_flag=True, help="Process at most one queued lab job and exit.")
@click.option("--poll-seconds", default=1.0, show_default=True, help="Idle poll interval in seconds.")
@click.option("--lease-seconds", default=90, show_default=True, help="Job lease duration in seconds.")
def lab_worker(once, poll_seconds, lease_seconds):
    """Run the isolated Regime Lab background worker."""
    import logging

    from forven.lab_dormancy import ensure_regime_lab_enabled
    from forven.lab_db import init_lab_db
    from forven.lab_worker_service import get_canonical_lab_worker_log_path, run_lab_worker_loop

    try:
        ensure_regime_lab_enabled(action="start the Regime Lab worker")
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    init_lab_db()
    log_path = get_canonical_lab_worker_log_path()
    # H-Op1: rotating file handler bounds log growth at 50 MiB x 3 backups.
    from forven.logging_config import setup_rotating_file_logger
    setup_rotating_file_logger(
        log_path,
        level=logging.INFO,
        fmt="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    click.echo("Regime Lab worker booting...", err=False)
    result = run_lab_worker_loop(
        once=bool(once),
        poll_interval_seconds=float(poll_seconds),
        lease_seconds=int(lease_seconds),
    )
    click.echo(f"Worker complete: {result}")


@lab.command("status")
def lab_status():
    """Show Regime Lab worker and queue status."""
    from rich.console import Console
    from rich.table import Table
    from forven.lab_db import init_lab_db
    from forven.lab_worker_service import get_lab_worker_status

    init_lab_db()
    console = Console()
    status = get_lab_worker_status()
    worker = status.get("worker", {}) if isinstance(status, dict) else {}

    console.print("\n[bold]Regime Lab Worker[/bold]\n")
    console.print(f"Active: {bool(status.get('active', False))}")
    if worker:
        console.print(f"State: {worker.get('state', '-')}")
        console.print(f"Worker ID: {worker.get('worker_id', '-')}")
        console.print(f"PID: {worker.get('pid', '-')}")
        console.print(f"Current Job: {worker.get('current_job_id') or '-'}")
        console.print(f"Last Job: {worker.get('last_job_id') or '-'}")

    running_jobs = list(status.get("running_jobs") or [])
    if running_jobs:
        table = Table(title="Running Lab Jobs")
        table.add_column("Job ID")
        table.add_column("Type")
        table.add_column("Worker")
        table.add_column("Progress")
        for job in running_jobs:
            progress = job.get("progress_json") or {}
            table.add_row(
                str(job.get("id") or ""),
                str(job.get("job_type") or ""),
                str(job.get("claimed_by") or "-"),
                str(progress.get("phase") or "-"),
            )
        console.print(table)


# --- Execution mode ---

@cli.group("execution")
def execution():
    """Execution mode and trading controls."""
    pass


@execution.command("mode")
@click.argument("mode", required=False)
def execution_mode(mode):
    """Get or set execution mode (paper/live).

    Paper mode: trades executed on HyperLiquid TESTNET (fake money).
    Live mode: trades executed on HyperLiquid TESTNET (mainnet reserved for future).
    """
    from rich.console import Console
    from forven.config import get_execution_mode, set_execution_mode

    console = Console()

    if not mode:
        current = get_execution_mode()
        color = "green" if current == "live" else "yellow"
        console.print(f"\nExecution mode: [{color}]{current.upper()}[/{color}]")
        if current == "paper":
            console.print("[dim]Trades execute on HyperLiquid testnet (fake money).[/dim]")
        else:
            console.print("[bold red]LIVE MODE — orders sent to HyperLiquid testnet[/bold red]")
        return

    if mode not in ("paper", "live"):
        console.print("[red]Invalid mode. Use 'paper' or 'live'.[/red]")
        return

    if mode == "live":
        console.print("\n[bold red]WARNING: Live mode sends REAL orders to HyperLiquid testnet.[/bold red]")
        console.print("The execution-trader agent will place market/limit orders on your behalf.")
        if not click.confirm("Are you sure you want to enable live trading?"):
            console.print("Cancelled.")
            return

    set_execution_mode(mode)
    color = "green" if mode == "live" else "yellow"
    console.print(f"\nExecution mode set to: [{color}]{mode.upper()}[/{color}]")


@execution.command("status")
def execution_status():
    """Show execution system status — mode, pending tasks, exchange positions."""
    from rich.console import Console
    from rich.table import Table
    from forven.config import get_execution_mode
    from forven.db import get_db, init_db

    init_db()
    console = Console()

    mode = get_execution_mode()
    color = "green" if mode == "live" else "yellow"
    console.print("\n[bold]Execution Status[/bold]")
    console.print(f"Mode: [{color}]{mode.upper()}[/{color}]\n")

    # Pending execution tasks
    with get_db() as conn:
        pending = conn.execute(
            "SELECT * FROM agent_tasks WHERE agent_id='execution-trader' AND status='pending' ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        running = conn.execute(
            "SELECT * FROM agent_tasks WHERE agent_id='execution-trader' AND status='running'"
        ).fetchall()
        recent = conn.execute(
            "SELECT * FROM agent_tasks WHERE agent_id='execution-trader' AND status IN ('done', 'failed') ORDER BY completed_at DESC LIMIT 5"
        ).fetchall()

    if pending:
        table = Table(title=f"Pending Execution Tasks ({len(pending)})")
        table.add_column("ID")
        table.add_column("Title")
        table.add_column("Created")
        for t in pending:
            table.add_row(str(t["id"]), t["title"][:60], t["created_at"])
        console.print(table)
    else:
        console.print("[dim]No pending execution tasks[/dim]")

    if running:
        console.print(f"[yellow]Running: {len(running)} task(s)[/yellow]")

    if recent:
        table = Table(title="Recent Executions")
        table.add_column("ID")
        table.add_column("Title")
        table.add_column("Status")
        table.add_column("Completed")
        for t in recent:
            status_color = "green" if t["status"] == "done" else "red"
            table.add_row(str(t["id"]), t["title"][:60], f"[{status_color}]{t['status']}[/{status_color}]", t["completed_at"] or "")
        console.print(table)

    # Exchange positions (testnet — shown for all modes)
    try:
        from forven.exchange.hyperliquid import get_positions, get_account_value
        acct = get_account_value(testnet=True)
        console.print("\n[bold]Exchange Account (Testnet)[/bold]")
        console.print(f"  Equity: ${acct['accountValue']:,.2f}")
        console.print(f"  Margin: ${acct['totalMarginUsed']:,.2f}")
        console.print(f"  Available: ${acct['totalRawUsd']:,.2f}")

        positions = get_positions(testnet=True)
        pos_list = positions.get("positions", [])
        active = [p for p in pos_list if float(p.get("position", p).get("szi", 0)) != 0]
        if active:
            table = Table(title="Exchange Positions (Testnet)")
            table.add_column("Asset")
            table.add_column("Direction")
            table.add_column("Size")
            table.add_column("Entry")
            table.add_column("uPnL")
            for pos in active:
                p = pos.get("position", pos)
                szi = float(p.get("szi", 0))
                table.add_row(
                    p.get("coin", ""),
                    "LONG" if szi > 0 else "SHORT",
                    f"{abs(szi):.6f}",
                    f"${float(p.get('entryPx', 0)):,.2f}",
                    f"${float(p.get('unrealizedPnl', 0)):,.2f}",
                )
            console.print(table)
        else:
            console.print("[dim]No open positions on exchange[/dim]")
    except Exception as e:
        console.print(f"[red]Exchange query failed: {e}[/red]")


@execution.command("reconcile")
def execution_reconcile():
    """Run position reconciliation between SQLite and exchange."""
    from rich.console import Console
    from forven.exchange.risk import reconcile_all_books
    from forven.db import init_db

    init_db()
    console = Console()

    result = reconcile_all_books()
    if result.get("error"):
        console.print(f"[red]{result['error']}[/red]")
        return

    if result.get("note"):
        console.print(f"[dim]{result['note']}[/dim]")
        return

    console.print("\n[bold]Position Reconciliation[/bold]")
    console.print(f"  SQLite open trades: {result['sqlite_open']}")
    console.print(f"  Exchange positions:  {result['exchange_open']}")

    if result["synced"]:
        console.print("[green]Positions are in sync.[/green]")
    else:
        console.print(f"\n[red]{len(result['discrepancies'])} discrepancies found:[/red]")
        for d in result["discrepancies"]:
            console.print(f"  [{d['type']}] {d['details']}")


# --- Strategy optimize (shortcut) ---

@strategies.command("optimize")
@click.argument("strategy_id")
def strategy_optimize(strategy_id):
    """Optimize parameters for a specific strategy."""
    from rich.console import Console
    from forven.strategies.optimizer import optimize_strategy
    from forven.db import init_db

    init_db()
    console = Console()
    console.print(f"Optimizing {strategy_id}...")

    result = optimize_strategy(strategy_id)
    if result.get("error"):
        console.print(f"[red]Error: {result['error']}[/red]")
        return

    console.print(f"\n[bold]Optimization Results for {strategy_id}[/bold]")
    console.print(f"  Best params: {result['best_params']}")
    console.print(f"  Fitness: {result['best_fitness']:.1f}")
    console.print(f"  WFA verdict: {result['wfa_verdict']}")
    console.print(f"  Degradation: {result.get('wfa_degradation', 'N/A')}")
    console.print(f"  Validated: {'Yes' if result['validated'] else 'No'}")

    if result.get("top_results"):
        console.print("\n[dim]Top 3 parameter sets:[/dim]")
        for i, r in enumerate(result["top_results"][:3], 1):
            console.print(f"  {i}. fitness={r['fitness']:.1f} | params={r['params']}")


# --- Status (full system overview) ---

@cli.command()
def status():
    """Full system status overview."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print("\n[bold]Forven System Status[/bold]\n")

    # Execution mode
    from forven.config import get_execution_mode
    mode = get_execution_mode()
    mode_color = "green" if mode == "live" else "yellow"
    console.print(f"Execution: [{mode_color}]{mode.upper()}[/{mode_color}]")

    # Auth
    from forven.auth.store import get_status_rows
    auth_table = Table(title="Auth Providers")
    auth_table.add_column("Provider", style="bold")
    auth_table.add_column("Status")
    auth_table.add_column("Expires")
    for row in get_status_rows():
        auth_table.add_row(*row)
    console.print(auth_table)

    # DB
    try:
        from forven.db import init_db, table_counts
        init_db()
        counts = table_counts()
        db_table = Table(title="Database")
        db_table.add_column("Table")
        db_table.add_column("Rows", justify="right")
        for name, count in counts.items():
            if count > 0:
                db_table.add_row(name, str(count))
        if any(c > 0 for c in counts.values()):
            console.print(db_table)
        else:
            console.print("[dim]Database: empty (run `forven db migrate` to import data)[/dim]")
    except Exception as e:
        console.print(f"[red]Database error: {e}[/red]")

    # Workspace
    from forven.workspace import list_workspace_files
    files = list_workspace_files()
    if files:
        console.print(f"\n[dim]Workspace: {len(files)} files[/dim]")
    else:
        console.print("[dim]Workspace: not initialized (run `forven workspace init`)[/dim]")


@cli.command("fts5-rebuild")
def fts5_rebuild():
    """Rebuild the Brain FTS5 recall indices from source tables.

    Repairs drift if a trigger was ever bypassed. Idempotent — safe to run
    anytime; will simply replay every source row into the index.
    """
    from forven.db import init_db, rebuild_fts5_indices

    init_db()
    counts = rebuild_fts5_indices()
    for name, count in counts.items():
        click.echo(f"  {name}: {count} rows")
    click.echo("FTS5 indices rebuilt.")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of formatted output")
def doctor(as_json):
    """Run health checks across DB / auth / scheduler / costs / interrupts."""
    import json as _json

    from forven.diagnostics import FAIL, PASS, WARN, snapshot

    payload = snapshot()
    if as_json:
        click.echo(_json.dumps(payload, indent=2))
        if payload["overall"] == FAIL:
            raise SystemExit(2)
        return

    from rich.console import Console
    from rich.table import Table

    console = Console()
    overall = payload["overall"]
    color = {PASS: "green", WARN: "yellow", FAIL: "red"}[overall]
    console.print(f"\n[bold {color}]forven doctor — overall: {overall.upper()}[/bold {color}]")
    console.print(
        f"  pass={payload['summary'].get(PASS, 0)}  "
        f"warn={payload['summary'].get(WARN, 0)}  "
        f"fail={payload['summary'].get(FAIL, 0)}\n"
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for check in payload["checks"]:
        s = check["status"]
        c = {PASS: "green", WARN: "yellow", FAIL: "red"}.get(s, "white")
        table.add_row(check["name"], f"[{c}]{s}[/{c}]", check["summary"])
    console.print(table)
    if overall == FAIL:
        raise SystemExit(2)


@cli.group()
def market_data():
    """Market data collection and backfill commands."""
    pass


@market_data.command("backfill")
@click.option("--asset", default=None, help="Asset to backfill (BTC, ETH, SOL). Omit for all.")
@click.option("--days", default=365, help="Days of history to fetch (default: 365).")
def market_data_backfill(asset, days):
    """Backfill historical funding rate data from HyperLiquid."""
    from forven.db import init_db
    from forven.market_data_collector import backfill_all, backfill_funding_history

    init_db()
    click.echo(f"Backfilling {asset or 'all assets'} — {days} days...")

    if asset:
        result = backfill_funding_history(asset, days_back=days)
        click.echo(f"  {asset}: {result['total_stored']} records stored (oldest: {result.get('oldest_record', '?')})")
    else:
        results = backfill_all(days_back=days)
        for a, result in results.items():
            click.echo(f"  {a}: {result['total_stored']} records stored (oldest: {result.get('oldest_record', '?')})")

    click.echo("Done.")


@market_data.command("collect")
def market_data_collect():
    """Collect current funding rates, OI, and mark prices."""
    from forven.db import init_db
    from forven.market_data_collector import collect_current_snapshot

    init_db()
    result = collect_current_snapshot()
    click.echo(f"Stored {result.get('stored', 0)} data points.")
    if result.get("errors"):
        click.echo(f"Errors: {result['errors']}")


@market_data.command("coverage")
@click.option("--asset", default=None, help="Filter by asset.")
def market_data_coverage(asset):
    """Show data coverage report."""
    from forven.db import init_db
    from forven.market_data_collector import get_data_coverage

    init_db()
    report = get_data_coverage(asset)
    if not report.get("coverage"):
        click.echo("No market data collected yet. Run: forven market-data backfill")
        return
    for entry in report["coverage"]:
        click.echo(
            f"  {entry['asset']:5s} {entry['metric_type']:20s} "
            f"| {entry['count']:6d} records "
            f"| {entry['earliest'][:10]} → {entry['latest'][:10]}"
        )


# --- Snapshot dump ---

@cli.command("dump")
@click.option(
    "--out",
    "-o",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Output JSON path (default: prints to stdout)",
)
def dump_cmd(out):
    """Dump a JSON snapshot bundle (profile + recent decisions/approvals).

    Excludes Brain memory and API keys for privacy/security.
    """
    import json
    from dataclasses import asdict
    from datetime import datetime, timezone

    from forven.db import get_db
    from forven.workspace import read_operator_profile

    profile = read_operator_profile()
    profile_dict = None
    if profile is not None:
        profile_dict = {
            "name": profile.name,
            "timezone": profile.timezone,
            "starting_capital_usd": profile.starting_capital_usd,
            "risk_per_trade_pct": profile.risk_per_trade_pct,
            "exchange": profile.exchange,
            "asset_universe": profile.asset_universe,
            "preferences": asdict(profile.preferences),
            "rules": list(profile.rules),
            "body_chars": len(profile.body or ""),
        }

    recent_decisions: list[dict] = []
    recent_approvals: list[dict] = []
    with get_db() as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='brain_decisions'"
        )
        if cur.fetchone():
            rows = conn.execute(
                "SELECT id, cycle_id, situation_summary, action_taken, outcome_observed, created_at "
                "FROM brain_decisions ORDER BY id DESC LIMIT 50"
            ).fetchall()
            recent_decisions = [dict(r) for r in rows]

        rows = conn.execute(
            "SELECT id, approval_type, status, target_type, target_id, created_at "
            "FROM approvals ORDER BY id DESC LIMIT 50"
        ).fetchall()
        recent_approvals = [dict(r) for r in rows]

    bundle = {
        "version": 7,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "profile": profile_dict,
        "recent_decisions": recent_decisions,
        "recent_approvals": recent_approvals,
    }

    serialized = json.dumps(bundle, indent=2, default=str)
    if out:
        from pathlib import Path

        Path(out).write_text(serialized, encoding="utf-8")
        click.echo(f"Wrote dump to {out}")
    else:
        click.echo(serialized)


if __name__ == "__main__":
    cli()
