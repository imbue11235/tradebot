"""
reporting/telegram.py — Telegram reporter + command listener.

Sends:
  - Trade execution alerts (immediate)
  - 12-hour status digests with P&L summary
  - Halt alerts if daily loss limit is hit
  - Session open/close notifications

Listens for commands (runs in background thread):
  "status"  → sends current status report immediately
  "help"    → lists available commands
"""

import logging
import threading
import requests
from datetime import datetime, timezone
from typing import Optional, Callable

logger = logging.getLogger("tradebot")


class TelegramReporter:
    def __init__(self, tg_cfg: dict):
        self.token = tg_cfg["bot_token"]
        self.chat_id = str(tg_cfg["chat_id"])
        self.report_on_trade = tg_cfg.get("report_on_trade", True)
        self._base_url = f"https://api.telegram.org/bot{self.token}"
        self._last_update_id: int = 0
        self._command_callback: Optional[Callable] = None
        self._listener_thread: Optional[threading.Thread] = None

    # ── Command listener ──────────────────────────────────────────────────

    def start_listener(self, on_status: Callable):
        """
        Start background thread that polls for incoming messages.
        on_status: callable with no args, returns None — engine calls
                   _send_status_report() when triggered.
        """
        self._command_callback = on_status
        # Clear any pending updates from before this session started
        self._clear_pending_updates()
        self._listener_thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-listener",
            daemon=True,  # Dies automatically when main process exits
        )
        self._listener_thread.start()
        logger.info("Telegram command listener started.")

    def _clear_pending_updates(self):
        """Drain any queued messages so we don't react to old ones on startup."""
        try:
            resp = requests.get(
                f"{self._base_url}/getUpdates",
                params={"timeout": 0, "offset": -1},
                timeout=10,
            )
            data = resp.json()
            updates = data.get("result", [])
            if updates:
                self._last_update_id = updates[-1]["update_id"] + 1
        except Exception:
            pass

    def _poll_loop(self):
        """Long-poll Telegram for new messages. Runs forever in background thread."""
        while True:
            try:
                resp = requests.get(
                    f"{self._base_url}/getUpdates",
                    params={
                        "timeout": 30,           # Long-poll: wait up to 30s for a message
                        "offset": self._last_update_id,
                        "allowed_updates": ["message"],
                    },
                    timeout=40,                  # HTTP timeout slightly longer than poll timeout
                )
                data = resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"] + 1
                    self._handle_update(update)
            except requests.exceptions.Timeout:
                pass  # Normal — long-poll expired with no messages
            except Exception as e:
                logger.debug(f"Telegram poll error: {e}")
                import time; time.sleep(5)

    def _handle_update(self, update: dict):
        """Process a single incoming message."""
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip().lower()

        # Only respond to messages from your own chat
        if chat_id != self.chat_id:
            logger.debug(f"Ignoring message from unknown chat_id={chat_id}")
            return

        logger.info(f"Telegram command received: '{text}'")

        if text in ("status", "/status"):
            self.send("⏳ Fetching status...")
            if self._command_callback:
                self._command_callback()

        elif text in ("help", "/help"):
            self.send(
                "🤖 <b>TRADEBOT COMMANDS</b>\n\n"
                "  <b>status</b> — Current account snapshot,\n"
                "             open positions &amp; today's P&amp;L\n"
                "  <b>help</b>   — Show this message"
            )

        else:
            self.send(
                f"❓ Unknown command: <i>{text[:50]}</i>\n"
                f"Type <b>help</b> to see available commands."
            )

    # ── Outbound messages ─────────────────────────────────────────────────

    def send(self, message: str):
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
        shares: float,
        price: float,
        confidence: float,
        headline: str,
        fees: float,
    ):
        if not self.report_on_trade:
            return
        emoji = "🟢" if side == "buy" else "🔴"
        qty_str = f"{shares:.4f}".rstrip('0').rstrip('.') if shares != int(shares) else str(int(shares))
        msg = (
            f"{emoji} <b>TRADE EXECUTED</b>\n"
            f"  {side.upper()} {qty_str}x <b>{ticker}</b> @ ${price:.2f}\n"
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
        shares: float,
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
        qty_str = f"{shares:.4f}".rstrip('0').rstrip('.') if shares != int(shares) else str(int(shares))
        msg = (
            f"{emoji} <b>POSITION CLOSED</b>\n"
            f"  {ticker}: {qty_str} shares\n"
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

        pos_lines = ""
        for p in open_positions:
            pnl = p.get("unrealized_pnl", 0)
            pct = p.get("unrealized_pnl_pct", 0) * 100
            emoji = "🟢" if pnl >= 0 else "🔴"
            qty_str = f"{p['qty']:.4f}".rstrip('0').rstrip('.') if p['qty'] != int(p['qty']) else str(int(p['qty']))
            pos_lines += f"  {emoji} {p['ticker']}: {qty_str} shares, P&L ${pnl:+.2f} ({pct:+.1f}%)\n"
        if not pos_lines:
            pos_lines = "  (none)\n"

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
        note = "State restored from checkpoint — continuing where I left off." if restarted else "Send <b>status</b> at any time for a live snapshot. 📬"
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
        mode = "📄 PAPER" if is_paper else "💵 LIVE"
        self.send(
            f"🔔 <b>TRADING SESSION OPEN</b> [{mode}]\n"
            f"  Market open buffer elapsed — actively scanning news\n"
            f"  Equity: ${equity:,.2f}\n"
            f"  Open positions carried over: {open_positions}\n"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    def session_close(self, equity: float, daily_pnl: float, trades_today: int, is_paper: bool):
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
