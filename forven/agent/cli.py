# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Forven agent CLI — drive the backend from any shell. JSON in, JSON out.

Designed for AI harnesses (Claude Code, Codex) that operate by running shell
commands: every subcommand prints a single JSON document to stdout and exits
0 on success / non-zero on error (message to stderr).

Examples:
    python -m forven.agent health
    python -m forven.agent context --out .tmp/ctx.json      # context is large
    python -m forven.agent list --status paper
    python -m forven.agent readiness --symbol BTC/USDT --timeframe 1h --streams funding,oi
    python -m forven.agent readiness --strategy S02545
    python -m forven.agent register --file /abs/path/strat.py --session ADZ-0001
    python -m forven.agent backtest --strategy S02545 --dataset BTC/USDT-1h --compact
    python -m forven.agent backtest --strategy S02545 --dataset ADA/USDT-1h \
        --trade-mode short_only --params '{"base_horizon":48}' --compact
    python -m forven.agent enqueue --file /abs/path/strat.py --dataset BTC/USDT-1h
    python -m forven.agent promote --strategy S02550 --to gauntlet --from quick_screen
    python -m forven.agent wait-paper --strategies S02545,S02604 --timeout 1800
    python -m forven.agent data-census --stream ohlcv --limit-worst 20
    python -m forven.agent breadth S12276            # frozen rule on 15 coins, sealed data
"""

from __future__ import annotations

import argparse
import json
import sys

from .client import ForvenAgentClient, ForvenAPIError


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")


def _params(s: str | None):
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--params is not valid JSON: {e}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forven-agent", description="Drive the Forven backend over HTTP.")
    p.add_argument("--base-url", default=None, help="backend origin (default env FORVEN_API_URL or http://127.0.0.1:8003)")
    p.add_argument("--api-key", default=None)
    p.add_argument("--operator-key", default=None)
    p.add_argument("--timeout", type=float, default=300.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    c = sub.add_parser("context"); c.add_argument("--out", help="write JSON here instead of stdout (it's large)")
    c = sub.add_parser("skills"); c.add_argument("--regime"); c.add_argument("--type"); c.add_argument("--limit", type=int, default=10)
    c = sub.add_parser("sessions"); c.add_argument("--limit", type=int, default=20)
    c = sub.add_parser("session"); c.add_argument("id")
    c = sub.add_parser("create-session"); c.add_argument("--label", default=""); c.add_argument("--actor", default="ai-agent"); c.add_argument("--objective", default="")
    c = sub.add_parser("close-session"); c.add_argument("id")
    c = sub.add_parser("list"); c.add_argument("--status", help="quick_screen|gauntlet|paper|archived|...")
    c = sub.add_parser("strategy"); c.add_argument("id")
    c = sub.add_parser("gate-report"); c.add_argument("id")
    c = sub.add_parser("readiness", help="data contract: is the data this strategy/idea needs stored, fit and current?")
    c.add_argument("--strategy"); c.add_argument("--symbol"); c.add_argument("--timeframe")
    c.add_argument("--streams", help="comma list: funding,oi,basis,iv,ls_ratio,taker,liquidations")
    c.add_argument("--history-days", type=int); c.add_argument("--strategy-type")
    c.add_argument("--code-file", help="draft strategy .py to scan for the feeds it reads (never executed)")
    c = sub.add_parser("status"); c.add_argument("ids", help="comma-separated strategy ids")
    c = sub.add_parser("runs"); c.add_argument("--limit", type=int, default=20)
    c = sub.add_parser("result"); c.add_argument("id")
    c = sub.add_parser("register"); c.add_argument("--file", required=True); c.add_argument("--session")
    c = sub.add_parser("backtest")
    c.add_argument("--strategy", required=True); c.add_argument("--dataset", required=True)
    c.add_argument("--trade-mode"); c.add_argument("--params"); c.add_argument("--leverage", type=float)
    c.add_argument("--timeframe"); c.add_argument("--session"); c.add_argument("--compact", action="store_true")
    c = sub.add_parser("optimize"); c.add_argument("--strategy", required=True); c.add_argument("--dataset", required=True)
    c.add_argument("--n-trials", type=int); c.add_argument("--objective"); c.add_argument("--ranges")
    c = sub.add_parser("verdict"); c.add_argument("--strategy", required=True); c.add_argument("--dataset", required=True); c.add_argument("--tests", help="comma list, e.g. walk_forward,cost_stress")
    c = sub.add_parser("promote"); c.add_argument("--strategy", required=True); c.add_argument("--to", required=True, dest="to_status")
    c.add_argument("--from", dest="from_status"); c.add_argument("--reason", default="ai_agent"); c.add_argument("--force", action="store_true")
    c = sub.add_parser("enqueue"); c.add_argument("--file", required=True); c.add_argument("--dataset", required=True)
    c.add_argument("--session"); c.add_argument("--trade-mode"); c.add_argument("--params")
    c = sub.add_parser("wait-paper"); c.add_argument("--strategies", required=True); c.add_argument("--timeout", type=float, default=3600.0); c.add_argument("--interval", type=float, default=90.0)
    c = sub.add_parser("data-census", help="freshness SLA census of every stored series (GET /api/data/sla)")
    c.add_argument("--stream", help="ohlcv|funding|oi|basis|iv|ls_ratio|taker|liquidations"); c.add_argument("--limit-worst", type=int, default=50)
    c = sub.add_parser("breadth", help="run a strategy's frozen rule on 15 coins over sealed research data")
    c.add_argument("id"); c.add_argument("--refresh", action="store_true"); c.add_argument("--no-wait", action="store_true")
    c.add_argument("--timeout", dest="wait_timeout", type=float, default=1800.0)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    fc = ForvenAgentClient(base_url=args.base_url, api_key=args.api_key,
                           operator_key=args.operator_key, timeout=args.timeout)
    try:
        cmd = args.cmd
        if cmd == "health":
            _emit(fc.health())
        elif cmd == "context":
            ctx = fc.get_context()
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    json.dump(ctx, f, indent=2, default=str)
                _emit({"written": args.out, "top_keys": list(ctx.keys()) if isinstance(ctx, dict) else None})
            else:
                _emit(ctx)
        elif cmd == "skills":
            _emit(fc.get_quant_skills(regime=args.regime, skill_type=args.type, limit=args.limit))
        elif cmd == "sessions":
            _emit(fc.list_sessions(limit=args.limit))
        elif cmd == "session":
            _emit(fc.get_session(args.id))
        elif cmd == "create-session":
            _emit(fc.create_session(label=args.label, actor=args.actor, objective=args.objective))
        elif cmd == "close-session":
            _emit(fc.close_session(args.id))
        elif cmd == "list":
            _emit(fc.list_strategies(status=args.status))
        elif cmd == "strategy":
            _emit(fc.get_strategy(args.id))
        elif cmd == "gate-report":
            _emit(fc.get_gate_report(args.id))
        elif cmd == "readiness":
            if not args.strategy and not (args.symbol and args.timeframe):
                raise SystemExit("readiness needs --strategy, or --symbol and --timeframe")
            code = None
            if args.code_file:
                with open(args.code_file, encoding="utf-8") as f:
                    code = f.read()
            streams = [s.strip() for s in args.streams.split(",") if s.strip()] if args.streams else None
            _emit(fc.get_data_readiness(args.strategy, symbol=args.symbol, timeframe=args.timeframe, streams=streams,
                                        history_days=args.history_days, strategy_type=args.strategy_type, code=code))
        elif cmd == "status":
            _emit([fc.get_status(s.strip()) for s in args.ids.split(",") if s.strip()])
        elif cmd == "runs":
            _emit(fc.get_recent_runs(limit=args.limit))
        elif cmd == "result":
            _emit(fc.get_result(args.id))
        elif cmd == "register":
            _emit(fc.register_file(args.file, session_id=args.session))
        elif cmd == "backtest":
            _emit(fc.run_backtest(args.strategy, args.dataset, trade_mode=args.trade_mode,
                                  parameters=_params(args.params), timeframe=args.timeframe,
                                  leverage=args.leverage, session_id=args.session, compact=args.compact))
        elif cmd == "optimize":
            _emit(fc.run_optimization(args.strategy, args.dataset, parameter_ranges=_params(args.ranges),
                                      objective=args.objective, n_trials=args.n_trials))
        elif cmd == "verdict":
            tests = [t.strip() for t in args.tests.split(",")] if args.tests else None
            _emit(fc.run_verdict(args.strategy, args.dataset, tests=tests))
        elif cmd == "promote":
            _emit(fc.promote(args.strategy, args.to_status, from_status=args.from_status,
                             reason=args.reason, force=args.force))
        elif cmd == "enqueue":
            _emit(fc.enqueue_candidate(args.file, args.dataset, session_id=args.session,
                                       trade_mode=args.trade_mode, parameters=_params(args.params)))
        elif cmd == "data-census":
            _emit(fc.data_census(stream=args.stream, limit_worst=args.limit_worst))
        elif cmd == "wait-paper":
            ids = [s.strip() for s in args.strategies.split(",") if s.strip()]
            _emit(fc.wait_for_paper(ids, timeout=args.timeout, interval=args.interval))
        elif cmd == "breadth":
            _emit(fc.breadth(args.id, refresh=args.refresh, wait=not args.no_wait, timeout=args.wait_timeout))
        else:
            print(f"unknown command {cmd}", file=sys.stderr)
            return 2
        return 0
    except ForvenAPIError as e:
        print(json.dumps({"error": "api_error", "status": e.status, "method": e.method,
                          "path": e.path, "body": e.body[:1000]}), file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - surface any failure as JSON to stderr
        print(json.dumps({"error": type(e).__name__, "message": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
