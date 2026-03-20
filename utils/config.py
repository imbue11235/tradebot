"""
utils/config.py — Loads and validates config.yaml
"""
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    _validate(cfg)
    return cfg


def _validate(cfg: dict):
    required = [
        ("alpaca.api_key",),
        ("alpaca.api_secret",),
        ("telegram.bot_token",),
        ("telegram.chat_id",),
    ]
    for keys in required:
        node = cfg
        for k in keys[0].split("."):
            node = node.get(k, {})
        if not node or node.startswith("YOUR_"):
            raise ValueError(
                f"Missing config value: {keys[0]}\n"
                f"Please edit config.yaml before running."
            )
