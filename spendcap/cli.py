"""Command line: spendcap serve | report | status"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .core import Config, Ledger


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="spendcap", description="Hard budgets for LLM API keys.")
    p.add_argument("-c", "--config", default="spendcap.toml", help="config file (default: spendcap.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the proxy")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    r = sub.add_parser("report", help="spend per key and model")
    r.add_argument("--days", type=float, default=30, help="look back this many days (default 30)")
    sub.add_parser("status", help="budget left for every key")
    args = p.parse_args(argv)

    try:
        cfg = Config.load(args.config)
    except FileNotFoundError:
        print(f"Config not found: {args.config}. Copy spendcap.example.toml to get started.", file=sys.stderr)
        return 2

    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        missing = [pr.api_key_env for pr in cfg.providers.values() if not pr.api_key]
        if missing:
            print(f"warning: env vars not set: {', '.join(missing)}", file=sys.stderr)
        uvicorn.run(create_app(cfg), host=args.host or cfg.host, port=args.port or cfg.port, log_level="warning")
        return 0

    ledger = Ledger(cfg.db)
    if args.cmd == "report":
        rows = ledger.report(time.time() - args.days * 86400)
        print(f"{'KEY':<18}{'MODEL':<36}{'CALLS':>7}{'IN':>12}{'OUT':>12}{'USD':>12}")
        total = 0.0
        for name, model, calls, tin, tout, cost in rows:
            total += cost or 0
            print(f"{name:<18}{model[:35]:<36}{calls:>7}{tin or 0:>12}{tout or 0:>12}{cost or 0:>12.4f}")
        print(f"{'TOTAL':<85}{total:>12.4f}")
    else:
        print(f"{'KEY':<18}{'PERIOD':<8}{'SPENT':>10}{'BUDGET':>10}{'LEFT':>10}")
        for k in cfg.keys.values():
            spent = ledger.spent(k.name, k.period_start())
            print(f"{k.name:<18}{k.period:<8}{spent:>10.4f}{k.budget_usd:>10.2f}{max(0, k.budget_usd - spent):>10.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
