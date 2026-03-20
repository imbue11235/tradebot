"""
core/fees.py — Accurate fee calculation for every trade.

Fees modelled:
  - SEC Section 31 fee   (sell side only, on notional value)
  - FINRA TAF            (sell side only, per share, capped)
  - Commission           (configurable, Alpaca = $0)
  - Currency conversion  (if your base currency is not USD)

All costs are deducted from P&L *before* deciding whether to trade.
The bot will not enter a position if expected fees exceed expected gain.
"""

from dataclasses import dataclass


@dataclass
class FeeEstimate:
    commission: float = 0.0
    sec_fee: float = 0.0
    finra_taf: float = 0.0
    fx_conversion: float = 0.0

    @property
    def total(self) -> float:
        return self.commission + self.sec_fee + self.finra_taf + self.fx_conversion

    def __str__(self):
        return (
            f"Fees: ${self.total:.4f} "
            f"(commission=${self.commission:.4f}, "
            f"SEC=${self.sec_fee:.4f}, "
            f"FINRA=${self.finra_taf:.4f}, "
            f"FX=${self.fx_conversion:.4f})"
        )


class FeeCalculator:
    def __init__(self, fee_cfg: dict):
        self.commission = fee_cfg.get("commission_per_trade_usd", 0.0)
        self.sec_rate = fee_cfg.get("sec_fee_per_dollar", 0.0000278)
        self.finra_rate = fee_cfg.get("finra_taf_per_share", 0.000166)
        self.finra_max = fee_cfg.get("finra_taf_max_usd", 8.30)
        self.fx_rate = fee_cfg.get("currency_conversion_fee_pct", 0.0)

    def estimate_round_trip(
        self,
        shares: float,
        entry_price: float,
        exit_price: float,
    ) -> FeeEstimate:
        """
        Full round-trip cost: buy + sell.
        SEC and FINRA fees only apply on the sell side.
        Commission applies to both legs.
        FX conversion applies to the initial buy (converting to USD).
        """
        buy_notional = shares * entry_price
        sell_notional = shares * exit_price

        commission = self.commission * 2  # buy + sell legs

        sec_fee = sell_notional * self.sec_rate

        finra_taf = min(shares * self.finra_rate, self.finra_max)

        fx_conversion = buy_notional * self.fx_rate

        return FeeEstimate(
            commission=commission,
            sec_fee=sec_fee,
            finra_taf=finra_taf,
            fx_conversion=fx_conversion,
        )

    def estimate_entry(self, shares: float, price: float) -> float:
        """Cost at entry only (commission + FX)."""
        return self.commission + (shares * price * self.fx_rate)

    def is_trade_viable(
        self,
        shares: float,
        entry_price: float,
        take_profit_pct: float,
        confidence: float,
    ) -> bool:
        """
        Returns False if fees would consume more than 50% of the
        expected gain, making the trade uneconomical.
        """
        expected_gain = shares * entry_price * take_profit_pct * confidence
        exit_price_estimate = entry_price * (1 + take_profit_pct)
        fees = self.estimate_round_trip(shares, entry_price, exit_price_estimate)
        return fees.total < (expected_gain * 0.5)
