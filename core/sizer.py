"""
core/sizer.py — Confidence-scaled position sizing.

Philosophy: concentrate in conviction, don't fill slots.

max_open_positions is a CEILING, not a target.
The bot should hold 1 large position at 0.95 confidence, not
8 small positions at 0.45 confidence each.

Sizing is purely confidence-driven:
  score → budget_pct (from tiers)
  target_usd = spendable * budget_pct

The slot_max cap is removed. Instead a single hard per-position cap
(max_position_pct) prevents catastrophic concentration, but high-conviction
trades are allowed to be meaningfully larger than low-conviction ones.

Additionally, a min_confidence_to_trade gate skips signals below a threshold
entirely — if nothing looks good, the bot stays in cash.
"""

import logging

logger = logging.getLogger("tradebot")


class PositionSizer:
    def __init__(self, sizing_cfg: dict, budget_cfg: dict, fee_calc, fractional: bool = False):
        self.tiers = sorted(
            sizing_cfg.get("tiers", []),
            key=lambda t: t["min_score"],
            reverse=True,
        )
        self.min_trade_usd = sizing_cfg.get("min_trade_usd", 20)
        self.max_total = budget_cfg.get("max_total_usd", 10000)
        self.reserve_pct = budget_cfg.get("reserve_pct", 0.05)
        self.max_positions = budget_cfg.get("max_open_positions", 8)

        # Hard cap per position as % of total spendable budget.
        # Prevents any single trade from using more than this, regardless of confidence.
        # Default 25% — a single 0.95-confidence trade can use up to 25% of budget.
        self.max_position_pct = sizing_cfg.get("max_position_pct", 0.25)

        self.fee_calc = fee_calc
        self.fractional = fractional

    def compute_shares(
        self,
        confidence: float,
        price: float,
        available_cash: float,
        open_positions: int,
    ) -> tuple[float, float, str]:
        """
        Returns (shares_to_buy, allocated_usd, reason_string).
        Returns (0, 0, reason) if the trade should be skipped.
        """
        spendable = min(available_cash, self.max_total) * (1 - self.reserve_pct)

        if open_positions >= self.max_positions:
            return 0, 0.0, "max open positions reached"

        # Confidence tier lookup — this drives sizing, not slot count
        budget_pct = self._tier_pct(confidence)
        if budget_pct == 0:
            return 0, 0.0, f"confidence {confidence:.2f} below all tiers — skipping"

        # Target is purely confidence-driven
        target_usd = spendable * budget_pct

        # Hard cap: no single position exceeds max_position_pct of budget
        # This replaces the old slot_max (spendable / max_positions)
        position_cap = spendable * self.max_position_pct
        target_usd = min(target_usd, position_cap)
        target_usd = min(target_usd, available_cash * 0.95)  # never go all-in

        if target_usd < self.min_trade_usd:
            return 0, 0.0, f"position too small (${target_usd:.2f} < min ${self.min_trade_usd})"

        if self.fractional:
            shares = round(target_usd / price, 6)
            if shares <= 0:
                return 0, 0.0, "computed fractional share quantity is zero"
        else:
            shares = int(target_usd / price)
            if shares < 1:
                return 0, 0.0, (
                    f"price ${price:.2f} too high for budget ${target_usd:.2f} "
                    f"(tip: set fractional_shares: true)"
                )

        actual_usd = shares * price

        if not self.fee_calc.is_trade_viable(shares, price, 0.04, confidence):
            return 0, 0.0, "fees would consume >50% of expected gain"

        frac_note = " (fractional)" if self.fractional and shares != int(shares) else ""
        reason = (
            f"confidence={confidence:.2f} → {budget_pct*100:.0f}% of budget "
            f"(cap={self.max_position_pct*100:.0f}%) → "
            f"{shares}{frac_note} shares @ ${price:.2f} = ${actual_usd:.2f}"
        )
        return shares, actual_usd, reason

    def _tier_pct(self, confidence: float) -> float:
        for tier in self.tiers:
            if tier["min_score"] <= confidence <= tier["max_score"]:
                return tier["budget_pct"]
        return 0.0
