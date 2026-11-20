"""Load config.toml and environment secrets."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"


@dataclass
class Secrets:
    finnhub: str | None = None
    anthropic: str | None = None
    discord: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        def get(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        return cls(
            finnhub=get("FINNHUB_API_KEY"),
            anthropic=get("ANTHROPIC_API_KEY"),
            discord=get("DISCORD_WEBHOOK_URL"),
        )


@dataclass
class Config:
    watchlist: list[str]
    index_tickers: list[str]
    schedule: dict
    analysis: dict
    news: dict
    llm: dict
    delivery: dict
    root: Path = field(default=ROOT)

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    watchlist = [t.strip().upper() for t in raw.get("watchlist", []) if t.strip()]
    # de-duplicate while keeping order
    seen: set[str] = set()
    watchlist = [t for t in watchlist if not (t in seen or seen.add(t))]
    for required in ("SPY", "QQQ"):
        if required not in watchlist:
            watchlist.append(required)

    index = [t.upper() for t in raw.get("index_tickers", ["SPY", "QQQ", "IWM"])]
    return Config(
        watchlist=watchlist,
        index_tickers=index,
        schedule=raw.get("schedule", {}),
        analysis=raw.get("analysis", {}),
        news=raw.get("news", {}),
        llm=raw.get("llm", {}),
        delivery=raw.get("delivery", {}),
        root=path.resolve().parent,
    )
