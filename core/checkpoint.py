"""
core/checkpoint.py — Persist and restore engine state across restarts.

Saves to logs/checkpoint.json after every significant event.
On startup, the engine reads this file and resumes where it left off.

What is checkpointed:
  - day_date             → correct new-day detection after restart
  - day_start_equity     → daily loss circuit breaker baseline preserved
  - realised_pnl_today   → today's P&L counter preserved
  - positions_metadata   → entry price, confidence, headline, entry_time
                           for all open positions (used for P&L logging)
  - seen_article_ids     → prevents re-acting on already-processed news
  - is_halted / reason   → halt state survives restart
  - last_report_time     → prevents double-sending the 12h report

Stale checkpoint detection:
  If the checkpoint is from a previous calendar day (UTC), it is
  discarded and a fresh day is started. This means an overnight
  restart always begins clean with correct baselines.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tradebot")

CHECKPOINT_VERSION = 2


class Checkpoint:
    def __init__(self, log_dir: str = "./logs"):
        self.path = Path(log_dir) / "checkpoint.json"
        Path(log_dir).mkdir(parents=True, exist_ok=True)

    # ── Save ──────────────────────────────────────────────────────────────

    def save(
        self,
        day_date: str,
        day_start_equity: float,
        realised_pnl_today: float,
        positions_metadata: dict,
        seen_article_ids: set,
        is_halted: bool,
        halt_reason: str,
        last_report_time: datetime,
    ):
        # Serialise entry_time datetimes to ISO strings
        serialised_meta = {}
        for ticker, meta in positions_metadata.items():
            m = dict(meta)
            if isinstance(m.get("entry_time"), datetime):
                m["entry_time"] = m["entry_time"].isoformat()
            serialised_meta[ticker] = m

        data = {
            "version": CHECKPOINT_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "day_date": day_date,
            "day_start_equity": day_start_equity,
            "realised_pnl_today": realised_pnl_today,
            "positions_metadata": serialised_meta,
            "seen_article_ids": list(seen_article_ids)[-500:],  # cap to 500
            "is_halted": is_halted,
            "halt_reason": halt_reason,
            "last_report_time": last_report_time.isoformat(),
        }
        try:
            self.path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Checkpoint save failed: {e}")

    # ── Load ──────────────────────────────────────────────────────────────

    def load(self) -> Optional[dict]:
        """
        Returns restored state dict, or None if no valid checkpoint exists.
        Discards checkpoint if it's from a previous calendar day.
        """
        if not self.path.exists():
            logger.info("No checkpoint found — starting fresh.")
            return None

        try:
            data = json.loads(self.path.read_text())
        except Exception as e:
            logger.warning(f"Checkpoint unreadable: {e} — starting fresh.")
            return None

        if data.get("version") != CHECKPOINT_VERSION:
            logger.warning("Checkpoint version mismatch — starting fresh.")
            return None

        # Discard if from a previous UTC day
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if data.get("day_date") != today:
            logger.info(f"Checkpoint is from {data.get('day_date')}, today is {today} — starting fresh day.")
            return None

        # Deserialise entry_time strings back to datetimes
        for ticker, meta in data.get("positions_metadata", {}).items():
            if isinstance(meta.get("entry_time"), str):
                try:
                    meta["entry_time"] = datetime.fromisoformat(meta["entry_time"])
                except Exception:
                    meta.pop("entry_time", None)

        # Deserialise last_report_time
        try:
            data["last_report_time"] = datetime.fromisoformat(data["last_report_time"])
        except Exception:
            data["last_report_time"] = None

        data["seen_article_ids"] = set(data.get("seen_article_ids", []))

        logger.info(
            f"Checkpoint restored from {data['saved_at']} | "
            f"P&L today: ${data['realised_pnl_today']:+.2f} | "
            f"positions tracked: {len(data['positions_metadata'])} | "
            f"articles seen: {len(data['seen_article_ids'])} | "
            f"halted: {data['is_halted']}"
        )
        return data

    def delete(self):
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass
