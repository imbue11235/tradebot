"""
strategies/orderflow.py — Microstructure order flow analysis.

Computes three signals from real market data for a given ticker:

1. DELTA
   The difference between aggressive buy volume and aggressive sell volume
   over a recent window of trades.
     - Trade at/above ask → aggressive buy (buyer hit the ask)
     - Trade at/below bid → aggressive sell (seller hit the bid)
   Positive delta = more buyers. Negative = more sellers.
   Normalised to [-1, +1] as delta_ratio = net_delta / total_volume.

2. BID/ASK IMBALANCE
   The size ratio at the top of the order book right now.
     imbalance = (bid_size - ask_size) / (bid_size + ask_size)
   Range: [-1, +1]. Positive = buyers queued up. Negative = sellers waiting.
   This is a leading indicator — it reflects intent, not just what has traded.

3. ABSORPTION
   Measures whether price is making progress relative to the delta being
   applied. If heavy buy delta is building but price isn't moving up,
   sellers are absorbing the buying — a bearish sign despite positive delta.
   Computed across multiple snapshots taken a few seconds apart.
     absorption_score > 0 → buyers absorbing sellers (bullish)
     absorption_score < 0 → sellers absorbing buyers (bearish)

COMPOSITE SCORE
   Weighted combination of all three:
     score = (delta_weight × delta_ratio)
           + (imbalance_weight × imbalance)
           + (absorption_weight × absorption_score)
   Range: [-1, +1]. Configurable weights and thresholds.

USAGE
   orderflow.analyse(ticker) → OrderFlowResult
   result.score        # composite [-1, +1]
   result.confirms_buy # True if score >= min_buy_score
   result.confirms_sell # True if score <= min_sell_score (veto on news exit)
   result.summary      # human-readable string for logging/telegram
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("tradebot")


@dataclass
class OrderFlowResult:
    ticker: str
    score: float                  # composite [-1, +1]
    delta_ratio: float            # net delta / total volume [-1, +1]
    imbalance: float              # bid/ask size imbalance [-1, +1]
    absorption_score: float       # absorption signal [-1, +1]
    trade_count: int              # trades analysed
    confirms_buy: bool
    confirms_sell_veto: bool      # True = order flow says DON'T sell (overrides news exit)
    reason: str                   # human-readable explanation

    @property
    def summary(self) -> str:
        direction = "BULLISH" if self.score > 0 else "BEARISH"
        return (
            f"{self.ticker} orderflow={self.score:+.2f} [{direction}] | "
            f"delta={self.delta_ratio:+.2f} "
            f"imbalance={self.imbalance:+.2f} "
            f"absorption={self.absorption_score:+.2f} "
            f"trades={self.trade_count}"
        )


class OrderFlowAnalyser:
    def __init__(self, cfg: dict, data_client):
        """
        cfg: the order_flow_confirmation block from config.yaml
        data_client: Alpaca StockHistoricalDataClient instance
        """
        self.data = data_client
        self.enabled = cfg.get("enabled", True)

        # Score thresholds
        self.min_buy_score = cfg.get("min_buy_score", 0.15)
        self.min_sell_veto_score = cfg.get("min_sell_veto_score", 0.20)

        # Component weights (must sum to 1.0)
        weights = cfg.get("weights", {})
        self.w_delta      = weights.get("delta",      0.50)
        self.w_imbalance  = weights.get("imbalance",  0.30)
        self.w_absorption = weights.get("absorption", 0.20)

        # Data window
        self.trade_window_minutes = cfg.get("trade_window_minutes", 5)
        self.min_trades = cfg.get("min_trades", 10)

        # Absorption: how many snapshots to take and interval between them
        self.absorption_snapshots = cfg.get("absorption_snapshots", 3)
        self.absorption_interval_sec = cfg.get("absorption_interval_sec", 2)

    def analyse(self, ticker: str) -> Optional[OrderFlowResult]:
        """
        Run full order flow analysis for a single ticker.
        Returns None if insufficient data or disabled.
        """
        if not self.enabled:
            return None

        try:
            # ── Step 1: fetch recent trades for delta calculation ──────────
            trades = self._fetch_trades(ticker)
            if len(trades) < self.min_trades:
                logger.debug(
                    f"OrderFlow {ticker}: only {len(trades)} trades "
                    f"(min {self.min_trades}) — skipping"
                )
                return None

            # ── Step 2: fetch current quote for imbalance ─────────────────
            quote = self._fetch_quote(ticker)
            if quote is None:
                return None

            # ── Step 3: compute delta ──────────────────────────────────────
            delta_ratio = self._compute_delta(trades, quote)

            # ── Step 4: compute bid/ask imbalance ─────────────────────────
            imbalance = self._compute_imbalance(quote)

            # ── Step 5: compute absorption ────────────────────────────────
            absorption = self._compute_absorption(ticker, quote)

            # ── Step 6: composite score ───────────────────────────────────
            score = (
                self.w_delta      * delta_ratio +
                self.w_imbalance  * imbalance +
                self.w_absorption * absorption
            )
            score = max(-1.0, min(1.0, score))  # clamp

            confirms_buy = score >= self.min_buy_score
            confirms_sell_veto = score >= self.min_sell_veto_score

            if confirms_buy:
                reason = f"order flow confirms buy (score={score:+.2f})"
            elif score < 0:
                reason = f"order flow bearish — buy skipped (score={score:+.2f})"
            else:
                reason = f"order flow neutral — buy skipped (score={score:+.2f}, need {self.min_buy_score})"

            result = OrderFlowResult(
                ticker=ticker,
                score=score,
                delta_ratio=delta_ratio,
                imbalance=imbalance,
                absorption_score=absorption,
                trade_count=len(trades),
                confirms_buy=confirms_buy,
                confirms_sell_veto=confirms_sell_veto,
                reason=reason,
            )
            logger.debug(result.summary)
            return result

        except Exception as e:
            logger.warning(f"OrderFlow analysis failed for {ticker}: {e}")
            return None

    # ── Delta ─────────────────────────────────────────────────────────────────

    def _compute_delta(self, trades: list, quote: dict) -> float:
        """
        Classify each trade as aggressive buy or sell using the Lee-Ready rule:
          price >= ask → buyer-initiated (aggressive buy)
          price <= bid → seller-initiated (aggressive sell)
          mid          → neutral (ignored)
        Returns net_delta / total_volume normalised to [-1, +1].
        """
        bid = quote["bid"]
        ask = quote["ask"]
        mid = (bid + ask) / 2

        buy_vol = 0.0
        sell_vol = 0.0

        for t in trades:
            price = t["price"]
            size = t["size"]
            if price >= ask:
                buy_vol += size
            elif price <= bid:
                sell_vol += size
            elif price > mid:
                # Tick rule fallback: slightly above mid = buy-side
                buy_vol += size * 0.5
            else:
                sell_vol += size * 0.5

        total = buy_vol + sell_vol
        if total == 0:
            return 0.0
        return (buy_vol - sell_vol) / total

    # ── Imbalance ─────────────────────────────────────────────────────────────

    def _compute_imbalance(self, quote: dict) -> float:
        """
        Bid/ask size imbalance at top of book.
        (bid_size - ask_size) / (bid_size + ask_size)
        """
        bid_size = quote.get("bid_size", 0)
        ask_size = quote.get("ask_size", 0)
        total = bid_size + ask_size
        if total == 0:
            return 0.0
        return (bid_size - ask_size) / total

    # ── Absorption ────────────────────────────────────────────────────────────

    def _compute_absorption(self, ticker: str, initial_quote: dict) -> float:
        """
        Take multiple quote snapshots and compare price movement to delta.
        If delta is positive but price doesn't rise → sellers absorbing buyers (bearish).
        If delta is negative but price doesn't fall → buyers absorbing sellers (bullish).

        Returns score in [-1, +1]:
          +1 = buyers absorbing sellers strongly (bullish absorption)
          -1 = sellers absorbing buyers strongly (bearish absorption)
           0 = price moving in line with delta (no absorption)
        """
        snapshots = [initial_quote]

        for _ in range(self.absorption_snapshots - 1):
            time.sleep(self.absorption_interval_sec)
            q = self._fetch_quote(ticker)
            if q:
                snapshots.append(q)

        if len(snapshots) < 2:
            return 0.0

        # Price change over snapshot window
        price_start = (snapshots[0]["bid"] + snapshots[0]["ask"]) / 2
        price_end   = (snapshots[-1]["bid"] + snapshots[-1]["ask"]) / 2
        price_delta = price_end - price_start
        price_pct   = price_delta / price_start if price_start > 0 else 0.0

        # Average delta ratio across snapshots
        avg_imbalance = sum(
            self._compute_imbalance(s) for s in snapshots
        ) / len(snapshots)

        # Absorption = delta pointing one way, price not following
        # If avg_imbalance > 0 (bid heavy) but price fell → bearish absorption
        # If avg_imbalance < 0 (ask heavy) but price rose → bullish absorption
        if abs(avg_imbalance) < 0.05:
            return 0.0  # too neutral to read absorption

        expected_direction = 1.0 if avg_imbalance > 0 else -1.0
        actual_direction = 1.0 if price_pct > 0 else (-1.0 if price_pct < 0 else 0.0)

        if expected_direction != actual_direction and actual_direction != 0:
            # Price moving against the order book pressure = absorption
            # The side being absorbed is the losing side
            # expected buy pressure but price down = sellers absorbing = bearish for buyer
            return -expected_direction * min(abs(avg_imbalance), 1.0)
        else:
            # Price confirming order book direction = no absorption, flow is clean
            return expected_direction * 0.3  # mild confirmation bonus

    # ── Data fetching ─────────────────────────────────────────────────────────

    def _fetch_trades(self, ticker: str) -> list:
        from alpaca.data.requests import StockTradesRequest

        since = datetime.now(timezone.utc) - timedelta(minutes=self.trade_window_minutes)
        req = StockTradesRequest(
            symbol_or_symbols=ticker,
            start=since,
            limit=1000,
        )
        trades_response = self.data.get_stock_trades(req)
        raw = trades_response.get(ticker, []) if hasattr(trades_response, "get") else []

        # Handle both dict and iterable response formats
        if not raw and hasattr(trades_response, "__iter__"):
            try:
                raw = list(trades_response[ticker])
            except (KeyError, TypeError):
                raw = []

        result = []
        for t in raw:
            try:
                result.append({
                    "price": float(t.price),
                    "size":  float(t.size),
                    "ts":    t.timestamp,
                })
            except Exception:
                continue
        return result

    def _fetch_quote(self, ticker: str) -> Optional[dict]:
        from alpaca.data.requests import StockLatestQuoteRequest

        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            quotes = self.data.get_stock_latest_quote(req)
            q = quotes[ticker]
            bid = float(q.bid_price or 0)
            ask = float(q.ask_price or 0)
            if bid <= 0 or ask <= 0:
                return None
            return {
                "bid":      bid,
                "ask":      ask,
                "bid_size": float(q.bid_size or 0),
                "ask_size": float(q.ask_size or 0),
                "mid":      (bid + ask) / 2,
            }
        except Exception as e:
            logger.debug(f"Quote fetch failed for {ticker}: {e}")
            return None
