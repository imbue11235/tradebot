"""
utils/config.py — Loads config.yaml and injects secrets from .env

Secrets never live in config.yaml. They are loaded from .env:
  ALPACA_API_KEY
  ALPACA_API_SECRET
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  NEWSAPI_KEY        (optional)
  FINNHUB_KEY        (optional)
"""
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv


def load_config(path: str = "config.yaml") -> dict:
    # Load .env from project root (same dir as config.yaml, or cwd)
    env_path = Path(path).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()  # fallback: search cwd and parents

    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    # Inject secrets from environment into the config dict
    _inject_env(cfg)
    _validate(cfg)
    return cfg


def _inject_env(cfg: dict):
    # Alpaca
    cfg.setdefault("alpaca", {})
    cfg["alpaca"]["api_key"]    = os.environ.get("ALPACA_API_KEY", "")
    cfg["alpaca"]["api_secret"] = os.environ.get("ALPACA_API_SECRET", "")

    # Telegram
    cfg.setdefault("telegram", {})
    cfg["telegram"]["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    cfg["telegram"]["chat_id"]   = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Optional news keys
    cfg.setdefault("news", {})
    cfg["news"]["newsapi_key"] = os.environ.get("NEWSAPI_KEY", "")
    cfg["news"]["finnhub_key"] = os.environ.get("FINNHUB_KEY", "")


def _validate(cfg: dict):
    missing = []

    if not cfg["alpaca"].get("api_key"):
        missing.append("ALPACA_API_KEY")
    if not cfg["alpaca"].get("api_secret"):
        missing.append("ALPACA_API_SECRET")
    if not cfg["telegram"].get("bot_token"):
        missing.append("TELEGRAM_BOT_TOKEN")
    if not cfg["telegram"].get("chat_id"):
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example → .env and fill in your values."
        )
