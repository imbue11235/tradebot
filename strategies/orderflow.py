"""
strategies/orderflow.py — Microstructure order flow analysis.

Computes three signals from real market data for a given ticker.
All three signals work on Alpaca's free plan.

NOTE ON SIP DATA TIMING:
  Alpaca's free plan enforces a 15-minute delay on SIP trade data.
  We work around this by fetching a window that ends ~15 minutes ago
  (omitting the end parameter so Alpaca defaults it to the allowed cutoff).
  With trade_window_minutes=5, trades from roughly 16-21 minutes ago are used.
  Quote and absorption signals use live data with no delay.

Signals computed:

1. DELTA
   Aggressive buy volume minus aggressive sell volume over the recent window.
   Trade at/above ask = buyer initiated. At/below bid = seller initiated.
   Normalised: delta_ratio = net_delta / total_volume ∈ [-1, +1]

2. BID/ASK IMBALANCE
   (bid_size - ask_size) / (bid_size + ask_size) ∈ [-1, +1]
   Live — reflects real-time order book pressure.
   Positive = buy-side pressure. Negative = sell-side pressure.

3. ABSORPTION
   Price movement vs order book pressure across multiple live snapshots.
   If heavy bid imbalance but price not rising → sellers absorbing → bearish.
   If heavy ask imbalance but price not falling → buyers absorbing → bullish.

COMPOSITE SCORE: weighted combination ∈ [-1, +1]
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
    score: float
    delta_ratio: float
    imbalance: float
    absorption_score: float
    trade_count: int
    confirms_buy: bool
    confirms_sell_veto: bool
    mode: str          # "full" or "quote_only"
    reason: str

    @property
    def summary(self) -> str:
        direction = "BULLISH" if self.score > 0 else "BEARISH"
        return (
            f"{self.ticker} OF={self.score:+.2f} [{direction}] [{self.mode}] | "
            f"delta={self.delta_ratio:+.2f} "
            f"imbalance={self.imbalance:+.2f} "
            f"absorption={self.absorption_score:+.2f}"
        )


class OrderFlowAnalyser:
    def __init__(self, cfg: dict, data_client):
        self.data = data_client
        self.enabled = cfg.get("enabled", True)
        self.use_trades = cfg.get("use_trades", True)  # set false to skip SIP entirely

        self.min_buy_score = cfg.get("min_buy_score", 0.15)
        self.min_sell_veto_score = cfg.get("min_sell_veto_score", 0.20)

        weights = cfg.get("weights", {})
        self.w_delta      = weights.get("delta",      0.50)
        self.w_imbalance  = weights.get("imbalance",  0.30)
        self.w_absorption = weights.get("absorption", 0.20)

        self.trade_window_minutes = cfg.get("trade_window_minutes", 5)
        self.min_trades = cfg.get("min_trades", 10)
        self.absorption_snapshots = cfg.get("absorption_snapshots", 3)
        self.absorption_interval_sec = cfg.get("absorption_interval_sec", 2)

    def analyse(self, ticker: str) -> Optional[OrderFlowResult]:
        if not self.enabled:
            return None

        try:
            quote = self._fetch_quote(ticker)
            if quote is None:
                return None

            # ── Try trade-based delta (requires paid SIP) ──────────────────
            delta_ratio = 0.0
            trade_count = 0
            mode = "quote_only"

            if self.use_trades:
                trades = self._fetch_trades(ticker)
                if trades:
                    delta_ratio = self._compute_delta(trades, quote)
                    trade_count = len(trades)
                    mode = "full"

            # In quote-only mode, re-weight to compensate for missing delta
            if mode == "quote_only":
                w_imbalance  = self.w_imbalance + (self.w_delta * 0.6)
                w_absorption = self.w_absorption + (self.w_delta * 0.4)
                w_delta = 0.0
            else:
                w_delta      = self.w_delta
                w_imbalance  = self.w_imbalance
                w_absorption = self.w_absorption

            imbalance  = self._compute_imbalance(quote)
            absorption = self._compute_absorption(ticker, quote)

            score = (
                w_delta      * delta_ratio +
                w_imbalance  * imbalance +
                w_absorption * absorption
            )
            score = max(-1.0, min(1.0, score))

            confirms_buy       = score >= self.min_buy_score
            confirms_sell_veto = score >= self.min_sell_veto_score

            if confirms_buy:
                reason = f"order flow confirms buy (score={score:+.2f}, {mode})"
            elif score < 0:
                reason = f"order flow bearish — skipped (score={score:+.2f}, {mode})"
            else:
                reason = f"order flow neutral — skipped (score={score:+.2f}, need {self.min_buy_score}, {mode})"

            result = OrderFlowResult(
                ticker=ticker,
                score=score,
                delta_ratio=delta_ratio,
                imbalance=imbalance,
                absorption_score=absorption,
                trade_count=trade_count,
                confirms_buy=confirms_buy,
                confirms_sell_veto=confirms_sell_veto,
                mode=mode,
                reason=reason,
            )
            logger.debug(result.summary)
            return result

        except Exception as e:
            logger.warning(f"OrderFlow analysis failed for {ticker}: {e}")
            return None

    # ── Delta ─────────────────────────────────────────────────────────────────

    def _compute_delta(self, trades: list, quote: dict) -> float:
        bid = quote["bid"]
        ask = quote["ask"]
        mid = (bid + ask) / 2
        buy_vol = sell_vol = 0.0

        for t in trades:
            price = t["price"]
            size  = t["size"]
            if price >= ask:
                buy_vol += size
            elif price <= bid:
                sell_vol += size
            elif price > mid:
                buy_vol += size * 0.5
            else:
                sell_vol += size * 0.5

        total = buy_vol + sell_vol
        return (buy_vol - sell_vol) / total if total else 0.0

    # ── Imbalance ─────────────────────────────────────────────────────────────

    def _compute_imbalance(self, quote: dict) -> float:
        bid_size = quote.get("bid_size", 0)
        ask_size = quote.get("ask_size", 0)
        total = bid_size + ask_size
        return (bid_size - ask_size) / total if total else 0.0

    # ── Absorption ────────────────────────────────────────────────────────────

    def _compute_absorption(self, ticker: str, initial_quote: dict) -> float:
        snapshots = [initial_quote]
        for _ in range(self.absorption_snapshots - 1):
            time.sleep(self.absorption_interval_sec)
            q = self._fetch_quote(ticker)
            if q:
                snapshots.append(q)

        if len(snapshots) < 2:
            return 0.0

        price_start   = (snapshots[0]["bid"] + snapshots[0]["ask"]) / 2
        price_end     = (snapshots[-1]["bid"] + snapshots[-1]["ask"]) / 2
        price_pct     = (price_end - price_start) / price_start if price_start else 0.0
        avg_imbalance = sum(self._compute_imbalance(s) for s in snapshots) / len(snapshots)

        if abs(avg_imbalance) < 0.05:
            return 0.0

        expected = 1.0 if avg_imbalance > 0 else -1.0
        actual   = 1.0 if price_pct > 0 else (-1.0 if price_pct < 0 else 0.0)

        if expected != actual and actual != 0:
            return -expected * min(abs(avg_imbalance), 1.0)
        else:
            return expected * 0.3

    # ── Data fetching ─────────────────────────────────────────────────────────

    def _fetch_trades(self, ticker: str) -> list:
        from alpaca.data.requests import StockTradesRequest

        try:
            # Free plan restriction: end must be >= 15 min in the past.
            # Fix: shift the window back 16 min and omit end entirely —
            # Alpaca defaults end to exactly the allowed cutoff.
            # e.g. trade_window_minutes=5 → fetches trades from -21min to -16min ago.
            SIP_LAG = 16  # minutes
            since = datetime.now(timezone.utc) - timedelta(
                minutes=self.trade_window_minutes + SIP_LAG
            )
            req = StockTradesRequest(symbol_or_symbols=ticker, start=since, limit=1000)
            resp = self.data.get_stock_trades(req)

            raw = []
            if hasattr(resp, "get"):
                raw = resp.get(ticker, [])
            if not raw and hasattr(resp, "__iter__"):
                try:
                    raw = list(resp[ticker])
                except (KeyError, TypeError):
                    pass

            result = []
            for t in raw:
                try:
                    result.append({"price": float(t.price), "size": float(t.size)})
                except Exception:
                    continue
            return result

        except Exception as e:
            logger.debug(f"Trade fetch failed for {ticker}: {e}")
            return []

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
