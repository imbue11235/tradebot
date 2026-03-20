"""
dashboard/state_writer.py — Writes live bot state to JSON for the dashboard.

The engine calls write_state() after every significant event.
The dashboard server reads this file periodically.

Also maintains equity_curve.jsonl for the intraday chart.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


class StateWriter:
    def __init__(self, log_dir: str = "./logs"):
        self.state_file = Path(log_dir) / "dashboard_state.json"
        self.equity_file = Path(log_dir) / "equity_curve.jsonl"
        Path(log_dir).mkdir(parents=True, exist_ok=True)

    def write(
        self,
        broker,
        risk,
        daily_pnl: float,
        budget_max: float,
        last_signals: list,
        status: str = "running",
    ):
        try:
            account = broker.get_account()
            positions = broker.get_open_positions()

            signals_data = []
            for s in last_signals[-10:]:
                signals_data.append({
                    "ticker": s.ticker,
                    "score": round(s.score, 3),
                    "headline": s.headline[:120],
                    "time": s.timestamp.isoformat(),
                })

            state = {
                "status": status,
                "is_paper": broker.paper,
                "is_halted": risk.is_halted,
                "halt_reason": risk.halt_reason,
                "account": account,
                "open_positions": positions,
                "last_signals": signals_data,
                "daily_pnl": round(daily_pnl, 2),
                "budget_max": budget_max,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            self.state_file.write_text(json.dumps(state, indent=2))

            # Append equity point for the chart
            with open(self.equity_file, "a") as f:
                f.write(json.dumps({
                    "t": datetime.now(timezone.utc).isoformat(),
                    "equity": account.get("equity", 0),
                    "cash": account.get("cash", 0),
                }) + "\n")

        except Exception as e:
            pass  # Never let dashboard writing crash the bot
