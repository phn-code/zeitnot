"""One move: forced moves, the ponder cache, difficulty, plan, main and backup calls, the second opinion and its
comparison, the deadline, and the note.

No move ever comes from code. The only move played without a model answer is the only legal move.
"""

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import chess

from . import compare, timeman
from .calls import MAIN, Calls, Decision, Spec
from .config import Level, Settings
from .context import FORCING_TASK, SYSTEM, Situation, build
from .difficulty import LegalMovesRater, Rater
from .engines import Answer, Call, Engine, EngineSet
from .events import CallEvent, DecisionEvent, ErrorEvent, Event, MoveEvent, NoteEvent, PlanEvent, Tokens
from .facts import Prior, PriorNote, compute, prior_note
from .ledger import SpendLedger
from .rules import board_from_moves, safe_moves
from .timeman import Clock

if TYPE_CHECKING:
    from .ponder import Ponderer

MAX_MAIN_ATTEMPTS = 3
SECOND = Spec("second_opinion", "Second opinion · most forcing", "most forcing")
BACKUP = Spec("backup", "Backup · safe moves only", "safe moves")


@dataclass(frozen=True)
class ThinkRequest:
    moves: tuple[str, ...]
    clock: Clock
    engine: str
    model: str | None = None
    time_control: str | None = None
    opinions: bool = False
    prepared: tuple[str, ...] = ()  # UCI moves pondering answered for replies the opponent did not play
    ponder_hits: int = 0  # the model's moves this game that played at once from pondering
    opponent_ms: int | None = None  # the opponent's median think time this game, for a ponder run's window


class NoteBook:
    """What the model's last move left for its next one, keyed by the UCI history ending with the model's move."""

    def __init__(self, size: int = 1024):
        self.size = size
        self.notes: OrderedDict[tuple[str, ...], Prior] = OrderedDict()

    def put(self, moves: tuple[str, ...], prior: Prior) -> None:
        self.notes[moves] = prior
        self.notes.move_to_end(moves)
        while len(self.notes) > self.size:
            self.notes.popitem(last=False)

    def get(self, moves: tuple[str, ...]) -> Prior | None:
        return self.notes.get(moves)


@dataclass
class Harness:
    engines: EngineSet
    ledger: SpendLedger
    settings: Settings
    notes: NoteBook = field(default_factory=NoteBook)
    ponder: "Ponderer | None" = None
    rater: Rater = field(default_factory=LegalMovesRater)


def _tokens(answers: list[Answer]) -> Tokens:
    return Tokens(
        input=sum(a.input_tokens for a in answers),
        thinking=sum(a.thinking_tokens for a in answers),
        output=sum(a.output_tokens for a in answers),
    )


def _append_log(path: Path | None, record: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _prior(notes: NoteBook, moves: tuple[str, ...], prepared: tuple[str, ...]) -> PriorNote | None:
    if len(moves) < 2:
        return None
    prior = notes.get(moves[:-1])
    if prior is None:
        return None
    before = board_from_moves(moves[:-1])
    return prior_note(before, prior, chess.Move.from_uci(moves[-1]), [chess.Move.from_uci(u) for u in prepared])


def _forced(board: chess.Board) -> MoveEvent | ErrorEvent | None:
    legal = list(board.legal_moves)
    if not legal:
        return ErrorEvent("the game is over, there is no move to make")
    if len(legal) == 1:
        return MoveEvent(legal[0].uci(), board.san(legal[0]), "forced", 0, 0.0, Tokens())
    return None


def _ponder_replies(board: chess.Board, d: Decision, calls: Calls, limit: int) -> list[chess.Move]:
    """Up to `limit` replies worth pondering after the decided move: its expected reply first, then other replies the
    analysis gave the opponent. Every one comes from a model answer."""
    after = board.copy()
    after.push(d.landed.move)
    seen = [d.landed.expected, *d.considered, *(calls.landed[k].expected for k in d.chosen[1:])]
    return [m for m in dict.fromkeys(seen) if m is not None and m in after.legal_moves][:limit]


async def one_move(req: ThinkRequest, h: Harness, started: float, ponder: str | None = None) -> AsyncIterator[Event]:
    """The pipeline for one position. It always ends in a MoveEvent or an ErrorEvent. `ponder` is the opponent's
    reply this run prepares for, in SAN, when it runs on the opponent's time. A ponder run on a hard position asks
    the second opinion too while settings.ponder_opinions is on, as it is by default."""
    board = board_from_moves(req.moves)
    if (done := _forced(board)) is not None:
        yield done
        return
    engine = h.engines[req.engine]
    if engine.paid and h.ledger.spent_usd >= h.settings.cap_usd:
        yield ErrorEvent(f"spend cap reached: ${h.ledger.spent_usd:.2f} of ${h.settings.cap_usd:.2f}")
        return

    facts = compute(board)
    prior = _prior(h.notes, req.moves, req.prepared)
    difficulty = await asyncio.to_thread(h.rater.rate, board.copy())
    hard = difficulty.tier == "hard"
    pondering = h.ponder is not None and h.settings.ponder.get(engine.name, False)
    instant = timeman.instant_share(req.ponder_hits, len(board.move_stack) // 2) if pondering else 0.0
    plan = timeman.plan(req.clock, len(board.move_stack), difficulty.tier, engine.latency, engine.main_levels, instant,
                        req.opponent_ms if ponder is not None else None)
    opinions = req.opinions and hard and (ponder is None or h.settings.ponder_opinions)
    second = timeman.second_level(plan.level, plan.budget_ms, engine.latency, engine.main_levels) if opinions else None
    yield PlanEvent(plan.budget_ms, plan.deadline_ms, plan.level, plan.reason, engine.name, difficulty.tier, difficulty.text,
                    difficulty.blunder_chance, opinions, second)
    if prior is not None:
        yield NoteEvent(f"as expected: {prior.actual}" if prior.hit else f"expected {prior.expected or 'no reply'}, got {prior.actual}")

    situation = Situation(req.clock, req.time_control, facts, prior)
    deadline_at = started + plan.deadline_ms / 1000
    budget_at = started + plan.budget_ms / 1000
    calls = Calls(board, ponder)
    holder: list[Decision] = []
    try:
        async for event in _main_and_backup(board, engine, situation, plan.level, calls, deadline_at, holder, second, budget_at):
            yield event
        if holder:
            d = holder[0]
            for event in calls.cancel_pending():
                yield event
            for key in d.chosen:
                for event in calls.mark(key, "chosen"):
                    yield event
            yield DecisionEvent(d.text, board.san(d.landed.move), calls.id(d.chosen[0]))
        elapsed_ms = int((time.monotonic() - started) * 1000)
    finally:
        tasks = calls.close()
        usd = sum(a.usd for a in calls.answers)
        h.ledger.add(usd)
        # Bookkeeping above is synchronous on purpose: when the client disconnects, the server cancels this
        # coroutine and every later await in the finally is cancelled again.
        await asyncio.gather(*tasks, return_exceptions=True)
    if not holder:
        return
    d = holder[0]
    move, answer = d.landed.move, d.landed.answer
    fen = board.fen()
    unplayed = tuple(dict.fromkeys(landed.move for key, landed in calls.landed.items()
                                   if calls.live[key].call.fen == fen and landed.move != move))
    h.notes.put(req.moves + (move.uci(),), Prior(answer.note, d.landed.expected, unplayed))
    after = board.copy()
    after.push(move)
    expected = after.san(d.landed.expected) if d.landed.expected else None
    yield MoveEvent(move.uci(), board.san(move), d.source, elapsed_ms, usd, _tokens(calls.answers), answer.note, False, expected,
                    tuple(m.uci() for m in _ponder_replies(board, d, calls, h.settings.ponder_replies)))


async def _main_and_backup(board: chess.Board, engine: Engine, situation: Situation, level: Level, calls: Calls,
                           deadline_at: float, out: list[Decision], second_level: Level | None,
                           budget_at: float) -> AsyncIterator[Event]:
    """The main call, the second opinion at `second_level` when it is set, and the backup when the main call thinks
    above the backup level, then the comparison on a split. The model's answer plays as it is: no rule overrides it. Ends with a
    Decision in `out`, or with an ErrorEvent when the main call failed every attempt."""
    legal = list(board.legal_moves)
    every = {board.san(m): m for m in legal}
    fen = board.fen()
    main_call = Call(SYSTEM, build(board, situation, legal), tuple(every), level, fen)
    calls.start("main", engine, main_call, every, MAIN)
    if second_level is not None:
        second_call = Call(SYSTEM, build(board, situation, legal, task=FORCING_TASK), tuple(every), second_level, fen)
        calls.start("second", engine, second_call, every, SECOND)
    if level != engine.backup_level:
        safe = safe_moves(board)
        by_safe = {board.san(m): m for m in safe}
        backup_call = Call(SYSTEM, build(board, situation, safe), tuple(by_safe), engine.backup_level, fen)
        calls.start("backup", engine, backup_call, by_safe, BACKUP)

    def play_backup(why: str) -> Decision:
        return Decision(calls.landed["backup"], "fallback", f"{why}, so the backup {board.san(calls.landed['backup'].move)}.", ("backup",))

    attempts = 1
    while True:
        async for event in calls.until(lambda: calls.resolved("main"), deadline_at):
            yield event
        if calls.pending("main"):
            # Past the deadline: the backup answer is played if one exists; otherwise the main call keeps going.
            async for event in calls.until(lambda: calls.resolved("main") or "backup" in calls.landed, None):
                yield event
            if "main" not in calls.landed and "backup" in calls.landed:
                for event in calls.cancel("main", timed_out=True):
                    yield event
                yield NoteEvent(f"playing backup move {board.san(calls.landed['backup'].move)}")
                out.append(play_backup("The main call ran past the deadline"))
                return
        if "main" in calls.landed:
            context: list[tuple[str, tuple[str, ...]]] = []
            if second_level is not None:
                async for event in _compare(board, engine, situation, calls, deadline_at, budget_at, out, context):
                    yield event
                if out:
                    return
            why, agree = context[0] if context else (f"The model chose {board.san(calls.landed['main'].move)}.", ())
            out.append(Decision(calls.landed["main"], "model", why, ("main", *agree)))
            return
        if calls.pending("backup") or "backup" in calls.landed:
            async for event in calls.until(lambda: calls.resolved("backup"), None):
                yield event
            if "backup" in calls.landed:
                yield NoteEvent(f"playing backup move {board.san(calls.landed['backup'].move)}")
                out.append(play_backup("The main call failed"))
                return
        if attempts >= MAX_MAIN_ATTEMPTS:
            if calls.pending("second"):
                async for event in calls.until(lambda: calls.resolved("second"), None):
                    yield event
            if "second" in calls.landed:
                second = calls.landed["second"]
                out.append(Decision(second, "model", f"The main call failed, so the second opinion {board.san(second.move)}.",
                                    ("second",)))
                return
            yield ErrorEvent(f"the model failed {attempts} times: {calls.failed.get('main')}")
            return
        attempts += 1
        yield NoteEvent(f"asking the model again (attempt {attempts} of {MAX_MAIN_ATTEMPTS})")
        calls.start("main", engine, main_call, every, replace(MAIN, label=f"Main · attempt {attempts}"))


async def _compare(board: chess.Board, engine: Engine, situation: Situation, calls: Calls, deadline_at: float,
                   budget_at: float, out: list[Decision], context: list[tuple[str, tuple[str, ...]]]) -> AsyncIterator[Event]:
    """The main answer has landed; wait for the second opinion. On a split, compare the two: the full comparison when
    two rounds of calls fit before the deadline, the quick one when one fits. The pick's Decision goes in `out`.
    Otherwise put the sentence for the main answer, and the keys of calls that agree with it, in `context`, and the
    main answer plays."""
    main = calls.landed["main"]
    a = board.san(main.move)
    # A main answer inside the budget waits for the second opinion only until the budget runs out, so a deeper
    # second opinion that runs long costs the budget at most. Past the point where not even the quick comparison
    # fits before the deadline, a later answer could not change the move, so the wait stops there too.
    in_budget = time.monotonic() < budget_at
    last_useful = deadline_at - compare.quick_ms(engine) / 1000
    wait_until = min(budget_at if in_budget else deadline_at, last_useful)
    async for event in calls.until(lambda: calls.resolved("second"), wait_until, note=False):
        yield event
    second = calls.landed.get("second")
    if second is None:
        failed = "second" in calls.failed and "second" not in calls.cancelled
        for event in calls.cancel("second"):
            yield event
        why = "failed" if failed else "did not answer in time"
        context.append((f"The second opinion {why}, so the main answer {a}.", ()))
        return
    b = board.san(second.move)
    if second.move == main.move:
        context.append((f"Both opinions chose {a}.", ("second",)))
        return
    left_ms = (deadline_at - time.monotonic()) * 1000
    if left_ms < compare.quick_ms(engine):
        yield NoteEvent(f"opinions split ({a} vs {b}) with no time to compare them")
        context.append((f"Opinions split ({a} vs {b}) with no time to compare, so the main answer {a}.", ()))
        return
    quick = left_ms < compare.full_ms(engine)
    if quick:
        yield NoteEvent(f"opinions split ({a} vs {b}); time for a quick comparison")
    compared: list[compare.Compared] = []
    async for event in compare.escalate(calls, engine, board, situation, [main, second], deadline_at, compared, quick):
        yield event
    found = compared[0]
    if found.pick is None:
        yield NoteEvent(f"{found.cut}; playing the main answer {a}")
        context.append((found.text(board, main.move), ()))
        return
    reply = found.replies.get(found.pick.move)
    agree = ("pick",) + tuple(k for k in ("main", "second") if calls.landed[k].move == found.pick.move)
    out.append(Decision(found.pick, "model", found.text(board, main.move), agree, considered=(reply,) if reply else ()))


async def think(req: ThinkRequest, h: Harness) -> AsyncIterator[Event]:
    """One move for /api/think: the ponder cache first, then the pipeline. Logs the move."""
    started = time.monotonic()
    plan: PlanEvent | None = None
    decision: DecisionEvent | None = None
    move: MoveEvent | None = None
    calls: dict[str, dict] = {}
    async with aclosing(_think_events(req, h, started)) as events:
        async for event in events:
            if isinstance(event, PlanEvent):
                plan = event
            elif isinstance(event, CallEvent):
                calls[event.call] = {"role": event.role, "label": event.label, "parent": event.parent, "status": event.status,
                                     "level": event.level, "move": event.move or calls.get(event.call, {}).get("move"),
                                     "elapsed_ms": event.elapsed_ms}
            elif isinstance(event, DecisionEvent):
                decision = event
            elif isinstance(event, MoveEvent):
                move = event
            yield event
    if move is None:
        return
    _append_log(h.settings.log_path, {
        "ts": time.time(), "engine": req.engine, "model": req.model, "moves": list(req.moves),
        "plan": asdict(plan) if plan else None,
        "decision": decision.text if decision else None, "ponder_hits": req.ponder_hits,
        "calls": [{"id": k, **v} for k, v in calls.items()], **asdict(move),
    })


async def _think_events(req: ThinkRequest, h: Harness, started: float) -> AsyncIterator[Event]:
    board = board_from_moves(req.moves)
    if _forced(board) is None and h.ponder is not None:
        base = board_from_moves(req.moves[:-1]).fen() if req.moves else None
        prepared = h.ponder.prepared(req.engine, base, board.fen()) if base else ()
        run = h.ponder.claim(req.engine, board.fen())
        if run is not None:
            try:
                async for event in run.follow():
                    yield event
            finally:
                if not run.done:  # the browser left while the run was still thinking: stop its calls too
                    run.cancel()
            done = run.result
            if done is not None:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                reply = board.pop()
                played = board.san(reply)
                board.push(reply)
                yield DecisionEvent(f"You played {played} as expected, so the prepared {done.san} plays at once.", done.san,
                                    run.decision.call if run.decision else None)
                yield replace(done, elapsed_ms=elapsed_ms, ponder_hit=True)
                return
            yield NoteEvent("pondering did not finish, thinking now")
        req = replace(req, prepared=prepared)
    async for event in one_move(req, h, started):
        yield event
