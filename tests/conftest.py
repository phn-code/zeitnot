import asyncio
from dataclasses import dataclass

import pytest

from zeitnot.config import Settings
from zeitnot.ledger import SpendLedger
from zeitnot.models import Answer, Thought
from zeitnot.player import ThinkRequest, think
from zeitnot.timeman import Clock

FAST_LATENCY = {"medium": 100, "low": 50, "minimal": 20}
# With FAST_LATENCY this gives a medium plan with a 700 ms deadline.
SHORT_DEADLINE_CLOCK = Clock(ai_ms=10_000, human_ms=10_000, increment_ms=2_000)
FORCED_HISTORY = ("a2a4", "g8h6", "b1a3", "h6f5", "c2c3", "c7c5", "a3c4", "f5h6", "c4d6")


@dataclass
class Script:
    """How a ScriptedClient behaves for one level."""

    san: str = "e4"
    delay_s: float = 0.0
    thoughts: tuple[str, ...] = ()
    error: Exception | None = None
    tokens: tuple[int, int, int] = (1000, 2000, 10)


class ScriptedClient:
    def __init__(self, main: Script | None = None, fallback: Script | None = None):
        self.main = main or Script()
        self.fallback = fallback or Script(san="Nf3", tokens=(500, 0, 5))
        self.log: list[str] = []

    @property
    def levels_called(self) -> list[str]:
        return [e.removeprefix("start:") for e in self.log if e.startswith("start:")]

    async def stream(self, prompt, choices, level):
        script = self.fallback if level == "minimal" else self.main
        self.log.append(f"start:{level}")
        self.last_choices = {level: list(choices)}
        try:
            for text in script.thoughts:
                await asyncio.sleep(script.delay_s / 10)
                yield Thought(text)
            await asyncio.sleep(script.delay_s)
            if script.error:
                raise script.error
            yield Answer(script.san, *script.tokens)
            self.log.append(f"done:{level}")
        except asyncio.CancelledError:
            self.log.append(f"cancelled:{level}")
            raise


@pytest.fixture
def settings(tmp_path):
    return Settings(
        cap_usd=10.0,
        ledger_path=tmp_path / "spend.json",
        log_path=tmp_path / "moves.jsonl",
        latency_p50_ms=dict(FAST_LATENCY),
    )


@pytest.fixture
def ledger(settings):
    return SpendLedger(settings.ledger_path)


async def run_think(client, ledger, settings, moves=(), clock=SHORT_DEADLINE_CLOCK, model="gemini-3.6-flash"):
    req = ThinkRequest(tuple(moves), clock, model)
    return [e async for e in think(req, client, ledger, settings)]
