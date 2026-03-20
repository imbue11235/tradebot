"""
reporting/telegram.py — Telegram status reporter.

Sends:
  - Trade execution alerts (immediate)
  - 12-hour status digests with P&L summary
  - Halt alerts if daily loss limit is hit
"""

import logging
import requests
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("tradebot")


class TelegramReporter:
    def __init__(self, tg_cfg: dict):
        self.token = tg_cfg["bot_token"]
        self.chat_id = tg_cfg["chat_id"]
        self.report_on_trade = tg_cfg.get("report_on_trade", True)
        self._base_url = f"https://api.telegram.org/bot{self.token}"

    def send(self, message: str):
        """Send a plain text message."""
        try:
            resp = requests.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")

    def trade_alert(
        self,
        side: str,
        ticker: str,
        shares: int,
        price: float,
        confidence: float,
        headline: str,
        fees: float,
    ):
        if not self.report_on_trade:
            return
        emoji = "🟢" if side == "buy" else "🔴"
        msg = (
            f"{emoji} <b>TRADE EXECUTED</b>\n"
            f"  {side.upper()} {shares}x <b>{ticker}</b> @ ${price:.2f}\n"
            f"  Notional: <b>${shares * price:,.2f}</b>\n"
            f"  Confidence: {confidence:.0%}\n"
            f"  Est. fees: ${fees:.4f}\n"
            f"  Signal: <i>\"{headline[:100]}\"</i>\n"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self.send(msg)

    def exit_alert(
        self,
        ticker: str,
        shares: int,
        entry: float,
        exit_price: float,
        net_pnl: float,
        reason: str,
        fees: float,
    ):
        if not self.report_on_trade:
            return
        emoji = "💰" if net_pnl > 0 else "📉"
        pct = ((exit_price - entry) / entry) * 100
        msg = (
            f"{emoji} <b>POSITION CLOSED</b>\n"
            f"  {ticker}: {shares} shares\n"
            f"  Entry ${entry:.2f} → Exit ${exit_price:.2f} ({pct:+.2f}%)\n"
            f"  Net P&amp;L: <b>${net_pnl:+.2f}</b>\n"
            f"  Fees: ${fees:.4f}\n"
            f"  Reason: {reason}\n"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self.send(msg)

    def status_report(
        self,
        account: dict,
        open_positions: list,
        trades_today: list,
        daily_pnl: float,
        is_paper: bool,
        is_halted: bool,
        halt_reason: str,
    ):
        mode = "📄 PAPER" if is_paper else "💵 LIVE"
        halt_str = f"\n⛔ <b>HALTED: {halt_reason}</b>" if is_halted else ""

        # Build position summary
        pos_lines = ""
        for p in open_positions:
            pnl = p.get("unrealized_pnl", 0)
            pct = p.get("unrealized_pnl_pct", 0) * 100
            emoji = "🟢" if pnl >= 0 else "🔴"
            pos_lines += f"  {emoji} {p['ticker']}: {p['qty']} shares, P&L ${pnl:+.2f} ({pct:+.1f}%)\n"
        if not pos_lines:
            pos_lines = "  (none)\n"

        # Trades today summary
        trade_count = len(trades_today)

        msg = (
            f"📊 <b>TRADEBOT STATUS REPORT</b> [{mode}]\n"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"{'─'*35}\n"
            f"💼 <b>Account</b>\n"
            f"  Equity:      ${account.get('equity', 0):>10,.2f}\n"
            f"  Cash:        ${account.get('cash', 0):>10,.2f}\n"
            f"  Portfolio:   ${account.get('portfolio_value', 0):>10,.2f}\n"
            f"{'─'*35}\n"
            f"📈 <b>Today</b>\n"
            f"  Trades executed: {trade_count}\n"
            f"  Realised P&amp;L:  ${daily_pnl:+,.2f}\n"
            f"{'─'*35}\n"
            f"🔓 <b>Open Positions ({len(open_positions)})</b>\n"
            f"{pos_lines}"
            f"{'─'*35}"
            f"{halt_str}"
        )
        self.send(msg)

    def startup_message(self, account: dict, budget_max: float, is_paper: bool, restarted: bool = False):
        mode = "📄 PAPER TRADING" if is_paper else "🔴 LIVE TRADING"
        icon = "🔄" if restarted else "🤖"
        title = "TRADEBOT RESUMED" if restarted else "TRADEBOT STARTED"
        note = "State restored from checkpoint — continuing where I left off." if restarted else "I will send trade alerts and status reports every 12 hours. 📬"
        msg = (
            f"{icon} <b>{title}</b> [{mode}]\n"
            f"  Equity: ${account.get('equity', 0):,.2f}\n"
            f"  Budget cap: ${budget_max:,.2f}\n"
            f"  Strategy: News/Sentiment (FinBERT)\n"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"{note}"
        )
        self.send(msg)

    def halt_alert(self, reason: str):
        self.send(
            f"🛑 <b>TRADING HALTED</b>\n"
            f"  Reason: {reason}\n"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"No further trades will be placed until the next trading day."
        )

    def session_open(self, equity: float, open_positions: int, is_paper: bool):
        """Sent when the trading window opens (market open + 15min buffer)."""
        mode = "📄 PAPER" if is_paper else "💵 LIVE"
        self.send(
            f"🔔 <b>TRADING SESSION OPEN</b> [{mode}]\n"
            f"  Market open buffer elapsed — actively scanning news\n"
            f"  Equity: ${equity:,.2f}\n"
            f"  Open positions carried over: {open_positions}\n"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    def session_close(
        self,
        equity: float,
        daily_pnl: float,
        trades_today: int,
        is_paper: bool,
    ):
        """Sent when the trading window closes (10min before market close)."""
        mode = "📄 PAPER" if is_paper else "💵 LIVE"
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        self.send(
            f"{pnl_emoji} <b>TRADING SESSION CLOSED</b> [{mode}]\n"
            f"  Closing all positions — market close in ~10 min\n"
            f"  Trades today: {trades_today}\n"
            f"  Realised P&amp;L today: <b>${daily_pnl:+,.2f}</b>\n"
            f"  Equity: ${equity:,.2f}\n"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
