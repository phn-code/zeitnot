import asyncio
from dataclasses import dataclass

import pytest

from zeitnot.config import Settings
from zeitnot.difficulty import Difficulty
from zeitnot.engines import Answer, Call, EngineSet, LatencyTable, Thought
from zeitnot.ledger import SpendLedger
from zeitnot.player import Harness, NoteBook, ThinkRequest, think
from zeitnot.ponder import Ponderer
from zeitnot.rules import board_from_moves
from zeitnot.timeman import Clock

FAST_LATENCY = {"low": (50, 100), "medium": (100, 300)}
# Easy start position: low, 120 ms deadline, no backup. Hard position: medium, 410 ms deadline, backup at low.
CLOCK = Clock(ai_ms=4_000, human_ms=4_000, increment_ms=0)
# 1. e4 d5: White's e4 pawn is attacked and undefended; ScriptedRater rates it hard. There 2. Qg4 hangs the queen
# to the c8 bishop, and Nc3 defends e4 and passes the rules check.
CRITICAL = ("e2e4", "d7d5")
FORCED_HISTORY = ("a2a4", "g8h6", "b1a3", "h6f5", "c2c3", "c7c5", "a3c4", "f5h6", "c4d6")
PRICE_PER_CALL = 0.001


@dataclass
class Script:
    """How a ScriptedEngine behaves for one role."""

    san: str = "e4"
    delay_s: float = 0.0
    thoughts: tuple[str, ...] = ()
    error: Exception | None = None
    fail_times: int = 0
    note: str | None = None
    expected: str | None = None
    tokens: tuple[int, int, int] = (1000, 2000, 10)


class ScriptedEngine:
    """Role by call: a call at the backup level is the backup when a backup script is given, and everything else is the
    main call."""

    name = "scripted"
    model = "scripted-1"
    label = "Scripted"
    paid = True
    main_levels = ("low", "medium")
    backup_level = "low"

    def __init__(self, main=None, backup=None, latency=None):
        self.scripts = {"main": main or Script(), "backup": backup}
        self.latency = LatencyTable(latency or FAST_LATENCY)
        self.log: list[str] = []
        self.calls: list[tuple[str, Call]] = []
        self.attempts: dict[str, int] = {}

    def role(self, call: Call) -> str:
        if call.level == self.backup_level and self.scripts["backup"] is not None and "main" in self.started_roles:
            return "backup"
        return "main"

    @property
    def started_roles(self) -> set[str]:
        return {r for r, _ in self.calls}

    def prompts(self, role: str) -> list[str]:
        return [c.prompt for r, c in self.calls if r == role]

    async def stream(self, call: Call):
        role = self.role(call)
        script = self.scripts[role]
        self.calls.append((role, call))
        self.attempts[role] = self.attempts.get(role, 0) + 1
        self.log.append(f"start:{role}")
        try:
            for text in script.thoughts:
                await asyncio.sleep(script.delay_s / 10)
                yield Thought(text)
            await asyncio.sleep(script.delay_s)
            if script.error and self.attempts[role] > script.fail_times:
                raise script.error
            if self.attempts[role] <= script.fail_times:
                raise RuntimeError(f"transient failure {self.attempts[role]}")
            yield Answer(script.san, script.note, *script.tokens, usd=PRICE_PER_CALL, expected_reply=script.expected)
            self.log.append(f"done:{role}")
        except asyncio.CancelledError:
            self.log.append(f"cancelled:{role}")
            raise


@pytest.fixture
def settings(tmp_path):
    return Settings(cap_usd=10.0, ledger_path=tmp_path / "spend.json", log_path=tmp_path / "moves.jsonl")


@pytest.fixture
def ledger(settings):
    return SpendLedger(settings.ledger_path)


class ScriptedRater:
    """Rates the positions after the given move lists hard and every other position easy."""

    name = "scripted"

    def __init__(self, hard=(CRITICAL,)):
        self.hard = {board_from_moves(moves).fen() for moves in hard}
        self.rated: list[str] = []

    def rate(self, board):
        self.rated.append(board.fen())
        tier = "hard" if board.fen() in self.hard else "easy"
        return Difficulty(tier, 0.3 if tier == "hard" else 0.0, f"scripted {tier}")


def harness(engine, ledger, settings, ponder=False, rater=None):
    return Harness(EngineSet({engine.name: engine}), ledger, settings, NoteBook(), Ponderer() if ponder else None,
                   rater or ScriptedRater())


async def run_think(engine, ledger, settings, moves=(), clock=CLOCK, h=None, opinions=False):
    h = h or harness(engine, ledger, settings)
    req = ThinkRequest(tuple(moves), clock, engine.name, engine.model, "1+0", opinions=opinions)
    return [e async for e in think(req, h)]
