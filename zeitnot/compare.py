"""Comparing two candidate moves when the main answer and the second opinion split.

`escalate` has two depths, chosen by the time left. The full comparison takes two rounds of calls: for each candidate
the harness plays it and a parallel call plays the opponent's strongest reply, the harness lists material and threats
after each reply, and one pick call chooses. The quick comparison takes one round: the harness lists the opponent's
checks and captures after each candidate, and the pick call chooses. The pick sees the candidates as other players'
suggestions.

Code never scores a line and never chooses a move. When the deadline cuts the comparison short, the caller plays
the best model answer it holds.
"""

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace

import chess

from .calls import Calls, Landed, Spec
from .config import Level
from .context import ANSWER, SYSTEM, Situation, build
from .engines import Call, Engine
from .events import Event
from .facts import compute, end_facts, line_facts, material, opponent_replies, san_with_number
from .timeman import Clock

LEVEL: Level = "low"
FULL_ROUNDS = 2  # replies, pick: each round waits for its slowest call
QUICK_ROUNDS = 1  # pick
PICK_TASK = ("Each line above plays one of the suggested moves, then the strongest reply another player found for the "
             "opponent, and lists facts about the position after it. Choose the move whose line is best for you in its "
             "worst case. " + ANSWER)
QUICK_PICK_TASK = ("Each line above plays one of the suggested moves and lists the opponent's checks and captures after "
                   "it. Work out the strongest of them for each move, then choose the move that is best for you in its "
                   "worst case. " + ANSWER)


@dataclass(frozen=True)
class Line:
    """One candidate, the opponent's reply a model call chose for it, and rules facts after that reply."""

    move: str
    uci: str
    reply: str | None = None
    reply_uci: str | None = None
    facts: tuple[str, ...] = ()
    balance: int | None = None  # material, yours minus theirs, after the reply; never used to choose
    forcing: tuple[str, ...] | None = None  # quick comparison: the opponent's checks and captures after the move


def full_ms(engine: Engine) -> int:
    return FULL_ROUNDS * engine.latency.p50(LEVEL)


def quick_ms(engine: Engine) -> int:
    return QUICK_ROUNDS * engine.latency.p50(LEVEL)


def lines_text(lines: Sequence[Line]) -> str:
    rows = []
    for i, line in enumerate(lines, 1):
        if line.forcing is not None:
            after = (f"After it the opponent can play: {', '.join(line.forcing)}." if line.forcing
                     else "After it the opponent has no checks or captures.")
            rows.append(f"Line {i}: {line.move}. {after}")
            continue
        reply = f"the opponent's strongest reply found: {line.reply}" if line.reply else "no reply found"
        rows.append(f"Line {i}: {line.move}, {reply}. After it: {'; '.join(line.facts) or 'no facts'}.")
    return "\n".join(rows)


def suggestion_text(sans: Sequence[str]) -> str:
    return (f"Other players suggest {', '.join(sans[:-1])} and {sans[-1]}. For each, another player found the opponent's "
            "strongest reply. The lines below list the facts after it.")


@dataclass
class Compared:
    """What `escalate` found: each candidate's line, the opponent's reply to each, and the pick if it landed."""

    lines: dict[chess.Move, Line]
    replies: dict[chess.Move, chess.Move] = field(default_factory=dict)
    pick: Landed | None = None
    cut: str | None = None  # why the comparison stopped short

    def text(self, board: chess.Board, held: chess.Move, who: str = "Opinions", fallback: str = "the main answer") -> str:
        """The decision sentence, for example "Opinions split (Bc4 vs Nf3). After Bc4 you would play Qb3, so Nf3."."""
        sans = [board.san(m) for m in self.lines]
        split = f"{who} split ({' vs '.join(sans)})"
        if self.pick is None:
            return f"{split}; {self.cut}, so {fallback} {board.san(held)}."
        chosen = self.pick.move
        others = [m for m in self.lines if m != chosen and m in self.replies]
        if not others:
            return f"{split}; the pick chose {board.san(chosen)}."
        after = board.copy()
        after.push(others[0])
        return f"{split}. After {board.san(others[0])} you would play {after.san(self.replies[others[0]])}, so {board.san(chosen)}."


async def escalate(calls: Calls, engine: Engine, board: chess.Board, situation: Situation,
                   candidates: Sequence[Landed], deadline_at: float, out: list[Compared],
                   quick: bool = False) -> AsyncIterator[Event]:
    """The full comparison (reply calls, then one pick call) or, when `quick`, the pick alone over the opponent's
    checks and captures after each candidate. Ends with one Compared in `out`."""
    moves = list(dict.fromkeys(c.move for c in candidates))
    proposer = {}
    for c in candidates:
        proposer.setdefault(c.move, c.key)
    found = Compared({m: Line(board.san(m), m.uci()) for m in moves})
    out.append(found)
    if quick:
        for move in moves:
            after = board.copy()
            after.push(move)
            if after.is_game_over():
                found.lines[move] = replace(found.lines[move], facts=end_facts(after))
            else:
                found.lines[move] = replace(found.lines[move], forcing=tuple(opponent_replies(board, move)))
        async for event in _pick(calls, engine, board, situation, moves, found, deadline_at, QUICK_PICK_TASK):
            yield event
        return

    started: dict[str, chess.Move] = {}
    for i, move in enumerate(moves):
        after = board.copy()
        after.push(move)
        if after.is_game_over():
            found.lines[move] = replace(found.lines[move], facts=end_facts(after))
            continue
        clock = Clock(situation.clock.human_ms, situation.clock.ai_ms, situation.clock.increment_ms)
        theirs = Situation(clock, situation.time_control, compute(after), None)
        replies = list(after.legal_moves)
        by_san = {after.san(m): m for m in replies}
        spec = Spec("reply", f"Your best reply to {board.san(move)}", "opponent's reply", calls.id(proposer[move]))
        key = f"reply{i}"
        calls.start(key, engine, Call(SYSTEM, build(after, theirs, replies), tuple(by_san), LEVEL, after.fen()), by_san, spec)
        started[key] = move
    shown: set[str] = set()

    def fresh() -> list[str]:
        return [k for k in started if k in calls.landed and k not in shown]

    while any(calls.pending(k) for k in started) and time.monotonic() < deadline_at:
        async for event in calls.until(lambda: bool(fresh()), deadline_at):
            yield event
        if new := fresh():
            for key in new:
                shown.add(key)
                move, reply = started[key], calls.landed[key].move
                after = board.copy()
                after.push(move)
                reply_san = san_with_number(after, reply)
                after.push(reply)
                balance = material(after, board.turn) - material(after, not board.turn)
                found.replies[move] = reply
                found.lines[move] = replace(found.lines[move], reply=reply_san, reply_uci=reply.uci(),
                                            facts=line_facts(after, board.turn), balance=balance)
    for key in started:
        for event in calls.cancel(key, timed_out=True):
            yield event

    if (deadline_at - time.monotonic()) * 1000 < engine.latency.p50(LEVEL):
        found.cut = "no time was left to compare the lines"
        return
    async for event in _pick(calls, engine, board, situation, moves, found, deadline_at, PICK_TASK):
        yield event


async def _pick(calls: Calls, engine: Engine, board: chess.Board, situation: Situation, moves: list[chess.Move],
                found: Compared, deadline_at: float, task: str) -> AsyncIterator[Event]:
    by_candidate = {board.san(m): m for m in moves}
    prompt = build(board, situation, moves, suggestion_text(list(by_candidate)), task,
                   lines=lines_text(list(found.lines.values())))
    spec = Spec("pick", f"Pick · {' or '.join(by_candidate)}", "pick")
    calls.start("pick", engine, Call(SYSTEM, prompt, tuple(by_candidate), LEVEL, board.fen()), by_candidate, spec)
    async for event in calls.until(lambda: calls.resolved("pick"), deadline_at):
        yield event
    if "pick" in calls.landed:
        found.pick = calls.landed["pick"]
        return
    for event in calls.cancel("pick", timed_out=True):
        yield event
    found.cut = "the pick did not finish"
