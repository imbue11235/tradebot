"""
core/engine.py — Main trading loop.

Runs continuously. Each iteration:
  1. Check if trading is allowed (hours, halt)
  2. Monitor open positions for stop/profit exits
  3. Fetch and score news signals
  4. For each signal: size, check viability, execute buy
  5. Periodic daily loss check
  6. Periodic status reports via Telegram
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("tradebot")


class TradingEngine:
    def __init__(self, cfg: dict, broker, risk, sizer, strategy, reporter, fee_calc, trade_logger):
        self.cfg = cfg
        self.broker = broker
        self.risk = risk
        self.sizer = sizer
        self.strategy = strategy
        self.reporter = reporter
        self.fee_calc = fee_calc
        self.trade_logger = trade_logger
        self._last_signals: list = []

        try:
            from dashboard.state_writer import StateWriter
            log_dir = cfg.get("logging", {}).get("log_dir", "./logs")
            self._state_writer = StateWriter(log_dir)
        except Exception:
            self._state_writer = None

        self.budget_max = cfg["budget"]["max_total_usd"]
        self.scan_interval_sec = 60          # Scan for news every 60 seconds
        self.risk_check_interval_sec = 30    # Check positions every 30 seconds
        self.report_interval_sec = cfg["telegram"]["report_interval_hours"] * 3600

        self._last_report_time = datetime.now(timezone.utc) - timedelta(hours=11)
        self._last_risk_check = datetime.now(timezone.utc)
        self._last_news_scan = datetime.now(timezone.utc) - timedelta(minutes=2)
        self._positions_metadata: dict = {}  # ticker → {confidence, headline, entry_price, entry_time}
        self._realised_pnl_today: float = 0.0
        self._day_date: Optional[str] = None

    def run(self):
        """Blocking main loop. Run this in a terminal or via systemd."""
        logger.info("=" * 55)
        logger.info("  TRADEBOT ENGINE STARTING")
        logger.info("=" * 55)

        # Startup
        account = self.broker.get_account()
        self.risk.record_day_start(account["equity"])
        self._day_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.reporter.startup_message(account, self.budget_max, self.broker.paper)

        logger.info(f"Account equity: ${account['equity']:,.2f} | Budget cap: ${self.budget_max:,.2f}")
        logger.info(f"Market open: {self.broker.is_market_open()}")

        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Shutdown requested. Closing positions...")
                self._close_all_positions("manual shutdown")
                logger.info("Goodbye.")
                break
            except Exception as e:
                logger.error(f"Unhandled error in main loop: {e}", exc_info=True)
                time.sleep(10)

    def _tick(self):
        now = datetime.now(timezone.utc)

        # ── New day reset ──────────────────────────────────────────────────
        today = now.strftime("%Y-%m-%d")
        if today != self._day_date:
            self._day_date = today
            account = self.broker.get_account()
            self.risk.record_day_start(account["equity"])
            self._realised_pnl_today = 0.0
            logger.info(f"New trading day: {today}")

        # ── Periodic risk check ────────────────────────────────────────────
        if (now - self._last_risk_check).total_seconds() >= self.risk_check_interval_sec:
            self._last_risk_check = now
            self._check_positions()

        # ── Daily loss circuit breaker ─────────────────────────────────────
        if not self.risk.is_halted:
            account = self.broker.get_account()
            if not self.risk.check_daily_loss(account["equity"]):
                self.reporter.halt_alert(self.risk.halt_reason)
                self._close_all_positions("daily loss limit hit")

        # ── Status report ─────────────────────────────────────────────────
        if (now - self._last_report_time).total_seconds() >= self.report_interval_sec:
            self._last_report_time = now
            self._send_status_report()

        # ── News scan ─────────────────────────────────────────────────────
        can_trade, reason = self.risk.can_trade()
        if (now - self._last_news_scan).total_seconds() >= self.scan_interval_sec:
            self._last_news_scan = now
            if can_trade:
                self._scan_and_trade()
            else:
                logger.debug(f"Skipping news scan: {reason}")

        time.sleep(5)

    def _check_positions(self):
        """Monitor all open positions for stop/profit triggers."""
        positions = self.broker.get_open_positions()
        for pos in positions:
            ticker = pos["ticker"]
            should_exit, reason = self.risk.should_exit(pos)
            if should_exit:
                logger.info(f"Exiting {ticker}: {reason}")
                self._close_position(ticker, pos, reason)

    def _scan_and_trade(self):
        """Fetch news signals and execute buys."""
        open_positions = self.broker.get_open_positions()
        open_tickers = {p["ticker"] for p in open_positions}
        open_count = len(open_positions)

        signals = self.strategy.fetch_signals()
        if signals:
            self._last_signals = (signals + self._last_signals)[:20]
        if self._state_writer:
            self._state_writer.write(self.broker, self.risk, self._realised_pnl_today,
                                     self.budget_max, self._last_signals)
        if not signals:
            logger.debug("No actionable signals this scan.")
            return

        available_cash = self.broker.get_available_cash(self.budget_max)

        for signal in signals:
            ticker = signal.ticker

            # Skip if we already hold this
            if ticker in open_tickers:
                logger.debug(f"Already holding {ticker}, skipping signal.")
                continue

            # Get current price
            price = self.broker.get_latest_price(ticker)
            if not price:
                logger.warning(f"Couldn't get price for {ticker}, skipping.")
                continue

            # Size the trade
            shares, allocated_usd, size_reason = self.sizer.compute_shares(
                confidence=signal.score,
                price=price,
                available_cash=available_cash,
                open_positions=open_count,
            )

            if shares == 0:
                logger.info(f"Skipping {ticker}: {size_reason}")
                continue

            # Fee estimate
            est_exit = price * (1 + self.cfg["risk"]["take_profit_pct"])
            fee_est = self.fee_calc.estimate_round_trip(shares, price, est_exit)

            logger.info(
                f"Executing BUY: {shares}x {ticker} @ ~${price:.2f} | {size_reason} | {fee_est}"
            )

            # Execute
            order = self.broker.buy(ticker, shares)
            if order:
                self._positions_metadata[ticker] = {
                    "confidence": signal.score,
                    "headline": signal.headline,
                    "entry_price": price,
                    "entry_time": datetime.now(timezone.utc),
                    "shares": shares,
                    "fee_entry": self.fee_calc.estimate_entry(shares, price),
                }
                open_tickers.add(ticker)
                open_count += 1
                available_cash -= allocated_usd

                self.reporter.trade_alert(
                    side="buy",
                    ticker=ticker,
                    shares=shares,
                    price=price,
                    confidence=signal.score,
                    headline=signal.headline,
                    fees=fee_est.total,
                )

    def _close_position(self, ticker: str, position: dict, reason: str):
        """Close a position and log the result."""
        meta = self._positions_metadata.get(ticker, {})
        entry_price = meta.get("entry_price", position["entry_price"])
        shares = position["qty"]
        exit_price = position["current_price"]

        fee_est = self.fee_calc.estimate_round_trip(shares, entry_price, exit_price)
        gross_pnl = (exit_price - entry_price) * shares
        net_pnl = gross_pnl - fee_est.total
        hold_minutes = 0
        if "entry_time" in meta:
            hold_minutes = int((datetime.now(timezone.utc) - meta["entry_time"]).total_seconds() / 60)

        order = self.broker.close_position(ticker)
        if order:
            self._realised_pnl_today += net_pnl
            self.reporter.exit_alert(
                ticker=ticker,
                shares=int(shares),
                entry=entry_price,
                exit_price=exit_price,
                net_pnl=net_pnl,
                reason=reason,
                fees=fee_est.total,
            )
            self.trade_logger.log({
                "ticker": ticker,
                "side": "sell",
                "qty": shares,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "gross_pnl": gross_pnl,
                "fees_usd": fee_est.total,
                "net_pnl": net_pnl,
                "confidence": meta.get("confidence", 0),
                "signal_reason": meta.get("headline", ""),
                "hold_minutes": hold_minutes,
            })
            self._positions_metadata.pop(ticker, None)

    def _close_all_positions(self, reason: str):
        for pos in self.broker.get_open_positions():
            self._close_position(pos["ticker"], pos, reason)

    def _send_status_report(self):
        account = self.broker.get_account()
        positions = self.broker.get_open_positions()
        trades_today = self.broker.get_closed_orders_today()
        self.reporter.status_report(
            account=account,
            open_positions=positions,
            trades_today=trades_today,
            daily_pnl=self._realised_pnl_today,
            is_paper=self.broker.paper,
            is_halted=self.risk.is_halted,
            halt_reason=self.risk.halt_reason,
        )
        logger.info("Status report sent to Telegram.")
