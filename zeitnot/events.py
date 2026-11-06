"""The NDJSON events /api/think streams, one dataclass per `type`."""

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

from .config import Level
from .difficulty import Tier

Source = Literal["model", "fallback", "forced"]
Role = Literal["main", "second_opinion", "backup", "recheck", "reply", "pick", "ponder"]
CallStatus = Literal["started", "answered", "cancelled", "failed", "chosen"]


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    thinking: int = 0
    output: int = 0


@dataclass(frozen=True)
class PlanEvent:
    budget_ms: int
    deadline_ms: int
    level: Level
    reason: str
    engine: str
    difficulty: Tier
    difficulty_text: str  # "1800 players blunder here 34% of the time" or "31 legal moves"
    blunder_chance: float | None
    opinions: bool = False
    second_level: Level | None = None  # the second opinion's level when it runs: the main level or one deeper
    type: Literal["plan"] = field(default="plan", init=False)


@dataclass(frozen=True)
class NoteEvent:
    text: str
    type: Literal["note"] = field(default="note", init=False)


@dataclass(frozen=True)
class CallEvent:
    """One step in the life of one model call. `call` is unique per server process, so a ponder run's calls keep
    their ids when /api/think replays them. `move` is the call's answer in SAN, set from `answered` on."""

    call: str
    status: CallStatus
    role: Role
    label: str
    parent: str | None
    engine: str
    level: Level
    framing: str
    fen: str  # the position the call answers in, so the browser can resolve the moves its commentary names
    elapsed_ms: int
    move: str | None = None
    uci: str | None = None
    expected_reply: str | None = None
    facts: tuple[str, ...] = ()  # rules facts after a reply call's answer; facts only, never a score
    error: str | None = None
    type: Literal["call"] = field(default="call", init=False)


@dataclass(frozen=True)
class ThoughtEvent:
    """A whole reasoning chunk when `stream` is None. Otherwise new commentary text for the chunk with that id."""

    text: str
    stream: str | None = None
    call: str | None = None
    type: Literal["thought"] = field(default="thought", init=False)


@dataclass(frozen=True)
class PulseEvent:
    """A heartbeat: the call is working. At most four a second per call."""

    call: str | None = None
    type: Literal["pulse"] = field(default="pulse", init=False)


@dataclass(frozen=True)
class DecisionEvent:
    """Why this move won, in one sentence. It comes just before the move."""

    text: str
    move: str
    call: str | None
    type: Literal["decision"] = field(default="decision", init=False)


@dataclass(frozen=True)
class MoveEvent:
    uci: str
    san: str
    source: Source
    elapsed_ms: int
    usd: float
    tokens: Tokens
    note: str | None = None
    ponder_hit: bool = False
    expected_reply: str | None = None  # SAN, legal after this move
    ponder_replies: tuple[str, ...] = ()  # UCI replies worth pondering, the expected one first
    type: Literal["move"] = field(default="move", init=False)


@dataclass(frozen=True)
class ErrorEvent:
    message: str
    type: Literal["error"] = field(default="error", init=False)


Event = PlanEvent | NoteEvent | CallEvent | ThoughtEvent | PulseEvent | DecisionEvent | MoveEvent | ErrorEvent


def to_ndjson(event: Event) -> str:
    return json.dumps(asdict(event)) + "\n"
