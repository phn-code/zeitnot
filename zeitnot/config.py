"""Settings, the price table and the latency table. Tables are data, not branches."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

Level = Literal["none", "minimal", "low", "medium", "high"]


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


# Introductory Gemini Flash pricing through 2026-12-31. Output includes thinking tokens.
_FLASH = Price(input_per_mtok=0.75, output_per_mtok=3.75)
PRICES: dict[str, Price] = {
    "gemini-3.6-flash": _FLASH,
    "gemini-3.7-flash": _FLASH,
    "gemini-3.8-flash": _FLASH,
}
DEFAULT_MODEL = "gemini-3.6-flash"

# p50 seconds-per-move in ms for gemini-3.6-flash on a billed key, 10 positions per level (experiments/p0_probe/FINDINGS.md).
LATENCY_P50_MS: dict[Level, int] = {"medium": 12_100, "low": 4_200, "minimal": 1_400}

# Levels the main call may use, best first.
MAIN_LEVELS: tuple[Level, ...] = ("medium", "low")

# Seconds the fake client waits before answering, per level.
FAKE_DELAY_S: dict[Level, float] = {"none": 0.0, "minimal": 0.1, "low": 1.5, "medium": 2.5, "high": 4.0}


@dataclass(frozen=True)
class Settings:
    api_key: str | None = field(default=None, repr=False)
    fake: bool = False
    cap_usd: float = 5.00
    ledger_path: Path | None = None
    log_path: Path | None = None
    models: tuple[str, ...] = tuple(PRICES)
    default_model: str = DEFAULT_MODEL
    latency_p50_ms: dict[Level, int] = field(default_factory=lambda: dict(LATENCY_P50_MS))
    fake_delay_s: dict[Level, float] = field(default_factory=lambda: dict(FAKE_DELAY_S))

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        fake = os.environ.get("ZEITNOT_FAKE_MODEL") == "1"
        return cls(
            api_key=os.environ.get("GEMINI_API_KEY"),
            fake=fake,
            cap_usd=float(os.environ.get("ZEITNOT_SPEND_CAP_USD", "5.00")),
            ledger_path=None if fake else ROOT / "data" / "spend.json",
            log_path=None if fake else ROOT / "logs" / "moves.jsonl",
        )
