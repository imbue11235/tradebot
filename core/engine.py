"""
core/engine.py — Main trading loop.

Runs continuously. Each iteration:
  1. Check if trading is allowed (hours, halt)
  2. Monitor open positions for stop/profit exits
  3. Fetch and score news signals
  4. For each signal: size, check viability, execute buy
  5. Periodic daily loss check
  6. Periodic status reports via Telegram

Crash-hardened via checkpoint.py — all volatile state is saved to
logs/checkpoint.json after every tick and restored on startup.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.checkpoint import Checkpoint

logger = logging.getLogger("tradebot")


class TradingEngine:
    def __init__(self, cfg: dict, broker, risk, sizer, strategy, orderflow, reporter, fee_calc, trade_logger):
        self.cfg = cfg
        self.broker = broker
        self.risk = risk
        self.sizer = sizer
        self.strategy = strategy
        self.reporter = reporter
        self.fee_calc = fee_calc
        self.trade_logger = trade_logger
        self.orderflow = orderflow
        self._last_signals: list = []

        log_dir = cfg.get("logging", {}).get("log_dir", "./logs")
        self.checkpoint = Checkpoint(log_dir)

        try:
            from dashboard.state_writer import StateWriter
            self._state_writer = StateWriter(log_dir)
        except Exception:
            self._state_writer = None

        self.budget_max = cfg["budget"]["max_total_usd"]
        neg_cfg = cfg.get("risk", {}).get("negative_news_exit", {})
        self._neg_news_enabled = neg_cfg.get("enabled", True)
        self._neg_news_min_score = neg_cfg.get("min_score", -0.60)
        self._neg_news_min_articles = neg_cfg.get("min_articles", 1)
        self.scan_interval_sec = 60
        self.risk_check_interval_sec = 30
        self.report_interval_sec = cfg["telegram"]["report_interval_hours"] * 3600

        # These will be overwritten by _restore_or_init()
        self._last_report_time = datetime.now(timezone.utc) - timedelta(hours=11)
        self._last_risk_check = datetime.now(timezone.utc)
        self._last_news_scan = datetime.now(timezone.utc) - timedelta(minutes=2)
        self._positions_metadata: dict = {}
        self._realised_pnl_today: float = 0.0
        self._day_date: Optional[str] = None

        # Session window state — tracks open/close alert transitions
        self._session_state: str = "unknown"  # "unknown" | "open" | "closed"

    # ── Startup ───────────────────────────────────────────────────────────

    def run(self):
        logger.info("=" * 55)
        logger.info("  TRADEBOT ENGINE STARTING")
        logger.info("=" * 55)

        account = self.broker.get_account()
        restarted = self._restore_or_init(account)

        self.reporter.startup_message(
            account, self.budget_max, self.broker.paper, restarted=restarted
        )
        logger.info(f"Account equity: ${account['equity']:,.2f} | Budget cap: ${self.budget_max:,.2f}")
        logger.info(f"Market open: {self.broker.is_market_open()}")

        # Start Telegram command listener in background thread
        self.reporter.start_listener(on_status=self._send_status_report)

        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Shutdown requested. Closing positions...")
                self._close_all_positions("manual shutdown")
                self.checkpoint.delete()
                logger.info("Goodbye.")
                break
            except Exception as e:
                logger.error(f"Unhandled error in main loop: {e}", exc_info=True)
                self._save_checkpoint()  # always checkpoint before sleeping on error
                time.sleep(10)

    def _recalc_pnl_from_csv(self) -> float:
        """
        Re-derive today's realised P&L by summing net_pnl from trades.csv.
        This is always authoritative — used instead of the checkpoint value
        to prevent stale or incorrect P&L figures surviving restarts.
        """
        import csv
        from pathlib import Path
        log_dir = self.cfg.get("logging", {}).get("log_dir", "./logs")
        csv_path = Path(self.cfg.get("logging", {}).get("trade_log_csv", f"{log_dir}/trades.csv"))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total = 0.0
        if not csv_path.exists():
            return total
        try:
            with open(csv_path, newline="") as f:
                for row in csv.DictReader(f):
                    # Only count sell rows from today
                    ts = row.get("timestamp", "")
                    if row.get("side") == "sell" and ts.startswith(today):
                        try:
                            total += float(row.get("net_pnl", 0) or 0)
                        except ValueError:
                            pass
        except Exception as e:
            logger.warning(f"Could not read trades CSV for P&L recalc: {e}")
        return total

    def _restore_or_init(self, account: dict) -> bool:
        """
        Try to restore state from checkpoint. Returns True if restored.
        Falls back to fresh init if checkpoint is missing/stale/corrupt.
        P&L is always recalculated from trades.csv — never trusted from checkpoint.
        """
        state = self.checkpoint.load()

        if state is not None:
            # Restore all volatile state
            self._day_date = state["day_date"]
            self._positions_metadata = state["positions_metadata"]
            self.strategy._seen_article_ids = state["seen_article_ids"]

            if state.get("last_report_time"):
                self._last_report_time = state["last_report_time"]

            # Restore risk manager state
            self.risk._day_start_equity = state["day_start_equity"]
            if state["is_halted"]:
                self.risk._halted = True
                self.risk._halt_reason = state["halt_reason"]
                logger.warning(f"Restored HALTED state: {state['halt_reason']}")

            # Reconcile: warn if Alpaca has positions we lost metadata for
            open_positions = self.broker.get_open_positions()
            open_tickers = {p["ticker"] for p in open_positions}
            tracked = set(self._positions_metadata.keys())
            untracked = open_tickers - tracked
            if untracked:
                logger.warning(
                    f"Open positions with no metadata (will use broker entry price): {untracked}"
                )

            # Always recalculate P&L from CSV — never trust checkpoint value
            # This corrects any bad P&L figures that may have been persisted
            csv_pnl = self._recalc_pnl_from_csv()
            if abs(csv_pnl - state["realised_pnl_today"]) > 0.01:
                logger.warning(
                    f"P&L mismatch: checkpoint had ${state['realised_pnl_today']:+.2f}, "
                    f"CSV recalculated ${csv_pnl:+.2f} — using CSV value"
                )
            self._realised_pnl_today = csv_pnl

            logger.info(
                f"Resumed from checkpoint | "
                f"P&L today (from CSV): ${self._realised_pnl_today:+.2f} | "
                f"day start equity: ${self.risk._day_start_equity:,.2f}"
            )
            return True

        else:
            # Fresh start
            self._day_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._realised_pnl_today = 0.0
            self._positions_metadata = {}
            self.risk.record_day_start(account["equity"])
            logger.info("Fresh start — no checkpoint to restore.")
            return False

    # ── Checkpoint save ───────────────────────────────────────────────────

    def _save_checkpoint(self):
        self.checkpoint.save(
            day_date=self._day_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            day_start_equity=self.risk._day_start_equity or 0.0,
            realised_pnl_today=self._realised_pnl_today,
            positions_metadata=self._positions_metadata,
            seen_article_ids=self.strategy._seen_article_ids,
            is_halted=self.risk.is_halted,
            halt_reason=self.risk.halt_reason,
            last_report_time=self._last_report_time,
        )

    # ── Main tick ─────────────────────────────────────────────────────────

    def _tick(self):
        now = datetime.now(timezone.utc)

        # ── New day reset ──────────────────────────────────────────────────
        today = now.strftime("%Y-%m-%d")
        if today != self._day_date:
            self._day_date = today
            account = self.broker.get_account()
            self.risk.record_day_start(account["equity"])
            self._realised_pnl_today = 0.0
            self._positions_metadata = {}
            self.strategy._seen_article_ids = {}
            logger.info(f"New trading day: {today} | Start equity: ${account['equity']:,.2f}")
            self._save_checkpoint()

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
                self._save_checkpoint()

        # ── Status report ─────────────────────────────────────────────────
        if (now - self._last_report_time).total_seconds() >= self.report_interval_sec:
            self._last_report_time = now
            self._send_status_report()
            self._save_checkpoint()

        # ── Session window open/close alerts ────────────────────────────────
        can_trade, reason = self.risk.can_trade()
        new_session_state = "open" if can_trade else "closed"
        if self._session_state != new_session_state:
            if new_session_state == "open" and self._session_state != "unknown":
                # Transition: closed → open (market opened + buffer elapsed)
                account = self.broker.get_account()
                open_count = len(self.broker.get_open_positions())
                logger.info("Trading session OPEN — scanning news")
                self.reporter.session_open(
                    equity=account["equity"],
                    open_positions=open_count,
                    is_paper=self.broker.paper,
                )
            elif new_session_state == "closed" and self._session_state == "open":
                # Transition: open → closed (approaching market close or market closed)
                account = self.broker.get_account()
                trades_today = self.broker.get_closed_orders_today()
                logger.info(f"Trading session CLOSED — reason: {reason}")
                self.reporter.session_close(
                    equity=account["equity"],
                    daily_pnl=self._realised_pnl_today,
                    trades_today=len(trades_today),
                    is_paper=self.broker.paper,
                )
            self._session_state = new_session_state

        # ── News scan ─────────────────────────────────────────────────────
        if (now - self._last_news_scan).total_seconds() >= self.scan_interval_sec:
            self._last_news_scan = now
            if can_trade:
                self._scan_and_trade()
            else:
                logger.debug(f"Skipping news scan: {reason}")

        # ── Checkpoint every tick ──────────────────────────────────────────
        self._save_checkpoint()

        time.sleep(5)

    # ── Position monitoring ───────────────────────────────────────────────

    def _check_positions(self):
        positions = self.broker.get_open_positions()
        for pos in positions:
            ticker = pos["ticker"]
            should_exit, reason = self.risk.should_exit(pos)
            if should_exit:
                logger.info(f"Exiting {ticker}: {reason}")
                self._close_position(ticker, pos, reason)

    # ── News scan & trade ─────────────────────────────────────────────────

    def _scan_and_trade(self):
        open_positions = self.broker.get_open_positions()
        open_tickers = {p["ticker"] for p in open_positions}
        open_count = len(open_positions)

        # ── Check held positions for negative news before buying anything new ──
        if self._neg_news_enabled and open_positions:
            self._check_negative_news(open_positions)
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

            if ticker in open_tickers:
                logger.debug(f"Already holding {ticker}, skipping signal.")
                continue

            price = self.broker.get_latest_price(ticker)
            if not price:
                logger.warning(f"Couldn't get price for {ticker}, skipping.")
                continue

            shares, allocated_usd, size_reason = self.sizer.compute_shares(
                confidence=signal.score,
                price=price,
                available_cash=available_cash,
                open_positions=open_count,
            )

            if shares == 0:
                logger.info(f"Skipping {ticker}: {size_reason}")
                continue

            est_exit = price * (1 + self.cfg["risk"]["take_profit_pct"])
            fee_est = self.fee_calc.estimate_round_trip(shares, price, est_exit)

            # ── Order flow confirmation gate ──────────────────────────
            of_result = self.orderflow.analyse(ticker)
            if of_result is not None and not of_result.confirms_buy:
                logger.info(
                    f"BUY VETOED by order flow: {ticker} | {of_result.reason}"
                )
                continue
            of_note = f"OF={of_result.score:+.2f}" if of_result else "OF=n/a"
            logger.info(
                f"Executing BUY: {shares}x {ticker} @ ~${price:.2f} | "
                f"{size_reason} | {fee_est} | {of_note}"
            )

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
                self._save_checkpoint()  # checkpoint immediately after each buy

    # ── Negative news exit ───────────────────────────────────────────────────

    def _check_negative_news(self, open_positions: list):
        held_tickers = {p["ticker"] for p in open_positions}
        neg_signals = self.strategy.fetch_negative_signals(
            held_tickers=held_tickers,
            min_score=self._neg_news_min_score,
            min_articles=self._neg_news_min_articles,
        )
        if not neg_signals:
            return

        pos_by_ticker = {p["ticker"]: p for p in open_positions}
        for ticker, (score, headline) in neg_signals.items():
            pos = pos_by_ticker.get(ticker)
            if not pos:
                continue

            # Order flow veto — if tape shows buyers absorbing the bad news, hold
            of_result = self.orderflow.analyse(ticker)
            if of_result is not None and of_result.confirms_sell_veto:
                logger.info(
                    f"News exit VETOED by order flow for {ticker}: "
                    f"news={score:.2f} but OF={of_result.score:+.2f} "
                    f"(buyers absorbing the bad news — holding)"
                )
                continue

            reason = f"negative news exit (score={score:.2f}): {headline[:80]}"
            logger.info(f"Exiting {ticker} on negative news | {reason}")
            self._close_position(ticker, pos, reason)
            self.reporter.send(
                f"\U0001f4f0 <b>NEWS EXIT</b>\n"
                f"  Selling <b>{ticker}</b> on negative news\n"
                f"  News score: {score:.2f} (threshold: {self._neg_news_min_score})\n"
                f"  Headline: <i>\"{headline[:120]}\"</i>\n"
                f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            )

    # ── Position closing ──────────────────────────────────────────────────

    def _close_position(self, ticker: str, position: dict, reason: str):
        meta = self._positions_metadata.get(ticker, {})
        entry_price = meta.get("entry_price", position["entry_price"])
        shares = position["qty"]

        # Derive exit price from unrealized_pnl / qty rather than current_price.
        # Alpaca's current_price field can return stale or incorrect values in
        # paper trading — unrealized_pnl is always calculated correctly server-side.
        unrealized_pnl = position.get("unrealized_pnl", 0.0)
        if shares and shares != 0:
            exit_price = entry_price + (unrealized_pnl / shares)
        else:
            exit_price = position["current_price"]  # fallback

        fee_est = self.fee_calc.estimate_round_trip(shares, entry_price, exit_price)
        gross_pnl = (exit_price - entry_price) * shares
        net_pnl = gross_pnl - fee_est.total
        hold_minutes = 0
        if "entry_time" in meta:
            hold_minutes = int(
                (datetime.now(timezone.utc) - meta["entry_time"]).total_seconds() / 60
            )

        order = self.broker.close_position(ticker)
        if order:
            self._realised_pnl_today += net_pnl
            self.reporter.exit_alert(
                ticker=ticker,
                shares=shares,
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
            self._save_checkpoint()  # checkpoint immediately after each sell

    def _close_all_positions(self, reason: str):
        for pos in self.broker.get_open_positions():
            self._close_position(pos["ticker"], pos, reason)

    # ── Status report ─────────────────────────────────────────────────────

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
