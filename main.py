#!/usr/bin/env python3
"""
main.py — TRADEBOT entry point.

Usage:
  python main.py                    # Run with config.yaml
  python main.py --config my.yaml   # Custom config file
  python main.py --dry-run          # Validate config and exit
  python main.py --status           # Print account status and exit
"""

import argparse
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from utils.config import load_config
from utils.logger import setup_logger, TradeCSVLogger
from core.broker import Broker
from core.fees import FeeCalculator
from core.sizer import PositionSizer
from core.risk import RiskManager
from core.engine import TradingEngine
from strategies.sentiment import SentimentStrategy
from reporting.telegram import TelegramReporter


def main():
    parser = argparse.ArgumentParser(description="Tradebot — News sentiment day trader")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and exit")
    parser.add_argument("--status", action="store_true", help="Print account status and exit")
    args = parser.parse_args()

    # ── Load config ───────────────────────────────────────────────────────
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"❌ Config error: {e}")
        sys.exit(1)

    # ── Logging ───────────────────────────────────────────────────────────
    log_cfg = cfg.get("logging", {})
    logger = setup_logger(log_cfg.get("log_dir", "./logs"), log_cfg.get("log_level", "INFO"))
    trade_logger = TradeCSVLogger(log_cfg.get("trade_log_csv", "./logs/trades.csv"))

    if args.dry_run:
        logger.info("✅ Config valid. Dry run complete.")
        sys.exit(0)

    # ── Build components ──────────────────────────────────────────────────
    broker = Broker(cfg)
    fee_calc = FeeCalculator(cfg["fees"])
    sizer = PositionSizer(cfg["sizing"], cfg["budget"], fee_calc)
    risk = RiskManager(cfg["risk"], broker)
    reporter = TelegramReporter(cfg["telegram"])
    strategy = SentimentStrategy(cfg["news"], broker.news_client, cfg["universe"])

    # ── Status mode ───────────────────────────────────────────────────────
    if args.status:
        account = broker.get_account()
        positions = broker.get_open_positions()
        print("\n── Account ─────────────────────────────")
        for k, v in account.items():
            print(f"  {k:20s}: {v}")
        print(f"\n── Open Positions ({len(positions)}) ──────────────────")
        for p in positions:
            print(
                f"  {p['ticker']:6s}  {p['qty']:>6} shares  "
                f"entry=${p['entry_price']:.2f}  "
                f"now=${p['current_price']:.2f}  "
                f"P&L=${p['unrealized_pnl']:+.2f} ({p['unrealized_pnl_pct']*100:+.1f}%)"
            )
        print()
        sys.exit(0)

    # ── Run ───────────────────────────────────────────────────────────────
    engine = TradingEngine(
        cfg=cfg,
        broker=broker,
        risk=risk,
        sizer=sizer,
        strategy=strategy,
        reporter=reporter,
        fee_calc=fee_calc,
        trade_logger=trade_logger,
    )
    engine.run()


if __name__ == "__main__":
    main()
