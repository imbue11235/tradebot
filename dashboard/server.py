"""
dashboard/server.py — Real-time web dashboard for tradebot.

Serves a terminal-style UI showing:
  - Account equity + P&L
  - Open positions with live unrealised P&L
  - Recent trade history from CSV log
  - Bot status (running, halted, paper/live)
  - News signals log
  - Equity curve (intraday)

Run alongside the bot:
  python dashboard/server.py --port 8080

Or add to your systemd setup as a second service.
Data is read from:
  - logs/trades.csv        (trade history)
  - logs/dashboard_state.json   (live state written by engine)
"""

import argparse
import json
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, send_from_directory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

app = Flask(__name__, static_folder="static")

STATE_FILE = Path("./logs/dashboard_state.json")
TRADES_CSV = Path("./logs/trades.csv")
EQUITY_LOG = Path("./logs/equity_curve.jsonl")


def read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "status": "unknown",
        "is_paper": True,
        "is_halted": False,
        "halt_reason": "",
        "account": {},
        "open_positions": [],
        "last_signals": [],
        "daily_pnl": 0.0,
        "budget_max": 0,
        "last_updated": None,
    }


def read_trades(limit: int = 50) -> list:
    if not TRADES_CSV.exists():
        return []
    try:
        with open(TRADES_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
        return list(reversed(rows[-limit:]))
    except Exception:
        return []


def read_equity_curve() -> list:
    if not EQUITY_LOG.exists():
        return []
    try:
        points = []
        for line in EQUITY_LOG.read_text().strip().splitlines()[-200:]:
            points.append(json.loads(line))
        return points
    except Exception:
        return []


# ── API endpoints ──────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    return jsonify(read_state())


@app.route("/api/trades")
def api_trades():
    return jsonify(read_trades())


@app.route("/api/equity")
def api_equity():
    return jsonify(read_equity_curve())


@app.route("/api/summary")
def api_summary():
    state = read_state()
    trades = read_trades(200)

    wins = [t for t in trades if float(t.get("net_pnl", 0)) > 0]
    losses = [t for t in trades if float(t.get("net_pnl", 0)) < 0]
    total_pnl = sum(float(t.get("net_pnl", 0)) for t in trades)
    total_fees = sum(float(t.get("fees_usd", 0)) for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(float(t["net_pnl"]) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(float(t["net_pnl"]) for t in losses) / len(losses) if losses else 0

    return jsonify({
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "daily_pnl": state.get("daily_pnl", 0),
        "account": state.get("account", {}),
        "is_paper": state.get("is_paper", True),
        "is_halted": state.get("is_halted", False),
        "halt_reason": state.get("halt_reason", ""),
        "status": state.get("status", "unknown"),
        "budget_max": state.get("budget_max", 0),
        "last_updated": state.get("last_updated"),
    })


@app.route("/healthz")
def healthz():
    """Health check endpoint — used by Docker and nginx to confirm Flask is up."""
    return "ok", 200


@app.route("/")
def index():
    return send_from_directory(Path(__file__).parent / "static", "index.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Dashboard running at http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
