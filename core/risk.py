"""
core/risk.py — Runtime risk management.

Enforces:
  - Stop-loss: close position if it drops X%
  - Take-profit: close position if it gains X%
  - Daily loss limit: halt all trading if total day loss exceeds threshold
  - Market hours guard: no trades outside window
  - Opening/closing buffer: avoid volatile open + illiquid close
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("tradebot")


class RiskManager:
    def __init__(self, risk_cfg: dict, broker):
        self.stop_loss = risk_cfg.get("stop_loss_pct", 0.025)
        self.take_profit = risk_cfg.get("take_profit_pct", 0.04)
        self.max_daily_loss_pct = risk_cfg.get("max_daily_loss_pct", 0.05)
        self.market_hours_only = risk_cfg.get("trade_only_market_hours", True)
        self.avoid_first_min = risk_cfg.get("avoid_first_minutes", 15)
        self.avoid_last_min = risk_cfg.get("avoid_last_minutes", 10)
        self.broker = broker

        self._day_start_equity: float | None = None
        self._halted: bool = False
        self._halt_reason: str = ""

    def record_day_start(self, equity: float):
        self._day_start_equity = equity
        self._halted = False
        self._halt_reason = ""
        logger.info(f"Day start equity recorded: ${equity:,.2f}")

    def can_trade(self) -> tuple[bool, str]:
        """Returns (True, '') if trading is allowed, else (False, reason)."""
        if self._halted:
            return False, f"HALTED: {self._halt_reason}"

        if self.market_hours_only and not self.broker.is_market_open():
            return False, "market closed"

        mins_to_close = self.broker.minutes_to_close()
        mins_to_open_end = None

        if self.broker.is_market_open():
            # Estimate minutes since open using minutes_to_close
            # Market is 6.5 hours = 390 minutes
            mins_elapsed = 390 - mins_to_close
            if mins_elapsed < self.avoid_first_min:
                return False, f"too close to open ({mins_elapsed}min elapsed, waiting {self.avoid_first_min})"
            if mins_to_close < self.avoid_last_min:
                return False, f"too close to close ({mins_to_close}min remaining)"

        return True, ""

    def check_daily_loss(self, current_equity: float) -> bool:
        """
        Returns False and halts if daily loss limit exceeded.
        Call this periodically during the trading day.
        """
        if self._day_start_equity is None:
            return True

        loss_pct = (self._day_start_equity - current_equity) / self._day_start_equity
        if loss_pct >= self.max_daily_loss_pct:
            self._halted = True
            self._halt_reason = (
                f"daily loss limit hit: -{loss_pct*100:.1f}% "
                f"(limit={self.max_daily_loss_pct*100:.1f}%)"
            )
            logger.critical(f"🛑 TRADING HALTED — {self._halt_reason}")
            return False
        return True

    def should_exit(self, position: dict) -> tuple[bool, str]:
        """
        Given an open position dict (from broker), decide if we should close.
        Returns (True, reason) or (False, '').
        """
        pnl_pct = position.get("unrealized_pnl_pct", 0.0)

        if pnl_pct <= -self.stop_loss:
            return True, f"stop-loss hit ({pnl_pct*100:.2f}%)"

        if pnl_pct >= self.take_profit:
            return True, f"take-profit hit ({pnl_pct*100:.2f}%)"

        # Force close before end of day
        mins_to_close = self.broker.minutes_to_close()
        if self.broker.is_market_open() and mins_to_close <= self.avoid_last_min:
            return True, f"EOD close ({mins_to_close}min to close)"

        return False, ""

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason
