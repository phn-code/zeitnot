"""Settings, the price table and the latency seeds. Tables are data, not branches."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# xhigh and max are in no engine's main_levels: the v4 allocator (docs/harness.md) decides when to use them.
Level = Literal["minimal", "low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


# Introductory Gemini Flash pricing through 2026-12-31. Output includes thinking tokens.
_FLASH = Price(input_per_mtok=0.75, output_per_mtok=3.75)
GEMINI_PRICES: dict[str, Price] = {
    "gemini-3.6-flash": _FLASH,
    "gemini-3.7-flash": _FLASH,
    "gemini-3.8-flash": _FLASH,
}
GEMINI_MODEL = "gemini-3.6-flash"
CODEX_MODEL = "gpt-6.1-sol"
CLAUDE_MODEL = "opus"

# (p50, p90) ms per level. Gemini: a billed key, 10 positions per level (experiments/p0_probe/FINDINGS.md).
# Codex: guesses from docs/harness.md that the live latency table corrects after a few calls. Its xhigh and max are
# unmeasured guesses too.
# Claude Opus low, medium and high: wall time of one stripped `claude -p` call per level on one position
# (experiments/claude_startup/out/runs.jsonl). The p90 values, and both values for xhigh and max, are guesses until
# experiments/harness_eval/effort.py measures them; the live table corrects them after a few calls. One call each on
# position g014 (out/effort.jsonl): xhigh answered in 8.2 s; max gave no answer within the 300 s timeout.
LATENCY_SEED: dict[str, dict[Level, tuple[int, int]]] = {
    "gemini": {"minimal": (1_400, 1_800), "low": (4_200, 7_300), "medium": (12_100, 26_900)},
    "codex": {"low": (10_000, 55_000), "medium": (25_000, 80_000), "xhigh": (60_000, 150_000), "max": (90_000, 200_000)},
    "claude": {"low": (7_900, 11_700), "medium": (12_000, 25_000), "high": (18_100, 40_200),
               "xhigh": (20_100, 117_300), "max": (300_000, 600_000)},
}

# The start screen's time control and speed line per engine. Claude's line quotes the measured seeds above.
DEFAULT_TIME_CONTROL: dict[str, tuple[float, int]] = {"claude": (5, 3), "codex": (15, 10), "gemini": (10, 5), "fake": (10, 5)}
SPEED_HINT: dict[str, str] = {
    "claude": "Claude Opus takes about 8 s on an easy move and longer on hard ones; it can lose on time at 1+0.",
    "codex": "Codex models take about 5 to 60 s per move; the model can lose on time at fast controls.",
    "fake": "Fake model: no API calls, no cost.",
}
# Shown on the start screen when neither subscription CLI is installed and logged in.
SETUP_HELP: dict[str, str] = {
    "claude": "Claude Opus: install Claude Code (npm install -g @anthropic-ai/claude-code), then run claude auth login.",
    "codex": "Codex Sol: install the Codex CLI (npm install -g @openai/codex), then run codex login.",
}
DETECT_TTL_S = 30.0

# Seconds the fake engine waits before answering, per level.
FAKE_DELAY_S: dict[Level, float] = {"minimal": 0.1, "low": 1.5, "medium": 2.5, "high": 4.0, "xhigh": 5.0, "max": 6.0}


@dataclass(frozen=True)
class Settings:
    api_key: str | None = field(default=None, repr=False)
    fake: bool = False
    gemini: bool = False
    codex_service_tier: str | None = None
    claude_bare: bool = False
    opinions: bool = True
    cap_usd: float = 5.00
    ledger_path: Path | None = None
    log_path: Path | None = None
    gemini_models: tuple[str, ...] = tuple(GEMINI_PRICES)
    fake_delay_s: dict[Level, float] = field(default_factory=lambda: dict(FAKE_DELAY_S))
    ponder: dict[str, bool] = field(default_factory=lambda: {"codex": True, "claude": True, "gemini": False, "fake": True})
    ponder_replies: int = 1  # replies pondered per turn, the expected one first
    ponder_opinions: bool = True  # whether a ponder run on a hard position asks a second opinion and compares a split
    maia: bool = False  # rate positions with Maia-2 when the maia extra is installed; the legal-move count otherwise
    maia_dir: Path = Path.home() / ".cache" / "zeitnot" / "maia2"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        fake = os.environ.get("ZEITNOT_FAKE_MODEL") == "1"
        gemini = os.environ.get("ZEITNOT_GEMINI") == "1"
        bare = os.environ.get("ZEITNOT_CLAUDE_BARE") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))
        return cls(
            api_key=os.environ.get("GEMINI_API_KEY"),
            fake=fake,
            gemini=gemini and not fake,
            codex_service_tier=os.environ.get("ZEITNOT_CODEX_SERVICE_TIER") or None,
            claude_bare=bare,
            opinions=os.environ.get("ZEITNOT_OPINIONS", "1") != "0",
            cap_usd=float(os.environ.get("ZEITNOT_SPEND_CAP_USD", "5.00")),
            ledger_path=None if fake else ROOT / "data" / "spend.json",
            fake_delay_s={level: s * float(os.environ.get("ZEITNOT_FAKE_DELAY_SCALE", "1")) for level, s in FAKE_DELAY_S.items()},
            log_path=None if fake else ROOT / "logs" / "moves.jsonl",
            ponder={"codex": True, "claude": os.environ.get("ZEITNOT_PONDER_CLAUDE", "1") == "1",
                    "gemini": os.environ.get("ZEITNOT_PONDER_GEMINI") == "1", "fake": True},
            ponder_replies=max(0, int(os.environ.get("ZEITNOT_PONDER_REPLIES", "1"))),
            ponder_opinions=os.environ.get("ZEITNOT_PONDER_OPINIONS", "1") != "0",
            maia=os.environ.get("ZEITNOT_MAIA", "1") != "0",
        )
