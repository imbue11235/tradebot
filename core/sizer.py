"""
core/sizer.py — Confidence-scaled position sizing.

Strategy:
  The bot scores each news signal 0.0→1.0 for confidence.
  That score maps to a % of available budget via configurable tiers.
  Kelly Criterion principles apply: size up on conviction, size down on uncertainty.

  Final size is further constrained by:
    - Hard budget cap
    - Per-position max (budget / max_open_positions)
    - Min trade size (to avoid fee erosion)
    - Available cash (never trade what you don't have)
"""

import logging

logger = logging.getLogger("tradebot")


class PositionSizer:
    def __init__(self, sizing_cfg: dict, budget_cfg: dict, fee_calc):
        self.tiers = sorted(
            sizing_cfg.get("tiers", []),
            key=lambda t: t["min_score"],
            reverse=True,
        )
        self.min_trade_usd = sizing_cfg.get("min_trade_usd", 20)
        self.max_total = budget_cfg.get("max_total_usd", 10000)
        self.reserve_pct = budget_cfg.get("reserve_pct", 0.05)
        self.max_positions = budget_cfg.get("max_open_positions", 8)
        self.fee_calc = fee_calc

    def compute_shares(
        self,
        confidence: float,
        price: float,
        available_cash: float,
        open_positions: int,
    ) -> tuple[int, float, str]:
        """
        Returns (shares_to_buy, allocated_usd, reason_string).
        Returns (0, 0, reason) if the trade should be skipped.
        """
        # Respect budget hard cap
        spendable = min(available_cash, self.max_total) * (1 - self.reserve_pct)

        if open_positions >= self.max_positions:
            return 0, 0.0, "max open positions reached"

        # Per-slot limit: don't concentrate more than 1/N of budget in one trade
        slot_max = spendable / self.max_positions

        # Confidence tier lookup
        budget_pct = self._tier_pct(confidence)
        if budget_pct == 0:
            return 0, 0.0, f"confidence {confidence:.2f} below all tiers"

        target_usd = spendable * budget_pct
        target_usd = min(target_usd, slot_max)  # cap to slot
        target_usd = min(target_usd, available_cash * 0.95)  # never go all-in

        if target_usd < self.min_trade_usd:
            return 0, 0.0, f"position too small (${target_usd:.2f} < min ${self.min_trade_usd})"

        shares = int(target_usd / price)
        if shares < 1:
            return 0, 0.0, f"price ${price:.2f} too high for budget ${target_usd:.2f}"

        actual_usd = shares * price

        # Fee viability check
        if not self.fee_calc.is_trade_viable(shares, price, 0.04, confidence):
            return 0, 0.0, "fees would consume >50% of expected gain"

        reason = (
            f"confidence={confidence:.2f} → tier {budget_pct*100:.0f}% of budget → "
            f"{shares} shares @ ${price:.2f} = ${actual_usd:.2f}"
        )
        return shares, actual_usd, reason

    def _tier_pct(self, confidence: float) -> float:
        for tier in self.tiers:
            if tier["min_score"] <= confidence <= tier["max_score"]:
                return tier["budget_pct"]
        return 0.0
