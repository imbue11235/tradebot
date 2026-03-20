"""
utils/logger.py — Structured coloured logging + CSV trade log
"""
import logging
import csv
import os
from datetime import datetime, timezone
from pathlib import Path
import colorlog


def setup_logger(log_dir: str, level: str = "INFO") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"tradebot_{datetime.now().strftime('%Y%m%d')}.log"

    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "white",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))

    logger = logging.getLogger("tradebot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.addHandler(handler)
    logger.addHandler(file_handler)
    return logger


class TradeCSVLogger:
    """Appends every executed trade to a CSV file for review."""

    FIELDS = [
        "timestamp", "ticker", "side", "qty", "entry_price",
        "exit_price", "gross_pnl", "fees_usd", "net_pnl",
        "confidence", "signal_reason", "hold_minutes"
    ]

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(path).exists():
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def log(self, record: dict):
        with open(self.path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore")
            writer.writerow({**record, "timestamp": datetime.now(timezone.utc).isoformat()})
