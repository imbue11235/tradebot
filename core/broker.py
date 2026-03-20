"""
core/broker.py — Alpaca broker interface.

Wraps alpaca-py to provide a clean interface for:
  - Account info / cash balance
  - Placing market orders (buy only — no shorts)
  - Listing open positions
  - Closing positions
  - Checking market hours
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient, NewsClient
from alpaca.data.requests import StockLatestQuoteRequest

logger = logging.getLogger("tradebot")


class Broker:
    def __init__(self, cfg: dict):
        alpaca_cfg = cfg["alpaca"]
        self.paper = alpaca_cfg.get("paper_trading", True)

        self.trading = TradingClient(
            api_key=alpaca_cfg["api_key"],
            secret_key=alpaca_cfg["api_secret"],
            paper=self.paper,
        )
        self.data = StockHistoricalDataClient(
            api_key=alpaca_cfg["api_key"],
            secret_key=alpaca_cfg["api_secret"],
        )
        self.news_client = NewsClient(
            api_key=alpaca_cfg["api_key"],
            secret_key=alpaca_cfg["api_secret"],
        )

        mode = "PAPER" if self.paper else "🔴 LIVE"
        logger.info(f"Broker connected [{mode}]")

    # ── Account ────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        acct = self.trading.get_account()
        return {
            "cash": float(acct.cash),
            "portfolio_value": float(acct.portfolio_value),
            "buying_power": float(acct.buying_power),
            "equity": float(acct.equity),
            "currency": acct.currency,
        }

    def get_available_cash(self, budget_max: float) -> float:
        acct = self.get_account()
        return min(acct["buying_power"], budget_max)

    # ── Market hours ───────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        clock = self.trading.get_clock()
        return clock.is_open

    def minutes_to_open(self) -> int:
        clock = self.trading.get_clock()
        delta = clock.next_open - clock.timestamp
        return max(0, int(delta.total_seconds() / 60))

    def minutes_to_close(self) -> int:
        clock = self.trading.get_clock()
        delta = clock.next_close - clock.timestamp
        return max(0, int(delta.total_seconds() / 60))

    # ── Quotes ─────────────────────────────────────────────────────────────

    def get_latest_price(self, ticker: str) -> Optional[float]:
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            quote = self.data.get_stock_latest_quote(req)
            q = quote[ticker]
            # Mid-price: average of bid and ask
            return (float(q.bid_price) + float(q.ask_price)) / 2
        except Exception as e:
            logger.warning(f"Failed to get price for {ticker}: {e}")
            return None

    # ── Orders ─────────────────────────────────────────────────────────────

    def buy(self, ticker: str, shares: float) -> Optional[dict]:
        """Place a market buy order. Accepts int (whole) or float (fractional) shares."""
        if shares <= 0:
            logger.warning(f"buy() called with shares={shares}, skipping")
            return None
        # Fractional orders must use TimeInForce.DAY and qty as float
        qty = shares  # Alpaca accepts float for fractional
        try:
            req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            order = self.trading.submit_order(req)
            logger.info(f"BUY order submitted: {shares} x {ticker} | id={order.id}")
            return {
                "id": str(order.id),
                "ticker": ticker,
                "qty": shares,
                "side": "buy",
                "status": str(order.status),
            }
        except Exception as e:
            logger.error(f"BUY order failed for {ticker}: {e}")
            return None

    def close_position(self, ticker: str) -> Optional[dict]:
        """Close entire position in a ticker at market."""
        try:
            order = self.trading.close_position(ticker)
            logger.info(f"SELL (close) submitted: {ticker} | id={order.id}")
            return {"id": str(order.id), "ticker": ticker, "side": "sell"}
        except Exception as e:
            logger.error(f"Close position failed for {ticker}: {e}")
            return None

    # ── Positions ──────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        positions = self.trading.get_all_positions()
        result = []
        for p in positions:
            result.append({
                "ticker": p.symbol,
                "qty": int(p.qty),
                "entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pnl": float(p.unrealized_pl),
                "unrealized_pnl_pct": float(p.unrealized_plpc),
            })
        return result

    def get_position(self, ticker: str) -> Optional[dict]:
        for p in self.get_open_positions():
            if p["ticker"] == ticker:
                return p
        return None

    # ── Trade history ──────────────────────────────────────────────────────

    def get_closed_orders_today(self) -> list[dict]:
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100)
            orders = self.trading.get_orders(req)
            today = datetime.now(timezone.utc).date()
            result = []
            for o in orders:
                if o.filled_at and o.filled_at.date() == today:
                    result.append({
                        "ticker": o.symbol,
                        "side": str(o.side),
                        "qty": float(o.filled_qty or 0),
                        "fill_price": float(o.filled_avg_price or 0),
                        "filled_at": o.filled_at.isoformat(),
                    })
            return result
        except Exception as e:
            logger.warning(f"Failed to fetch order history: {e}")
            return []
