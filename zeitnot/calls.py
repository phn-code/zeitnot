"""The model calls of one move: who each call is (id, role, label, parent), tasks streaming into one queue, what
landed or failed, and the call lifecycle events they produce."""

import asyncio
import itertools
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field, replace

import chess

from .engines import Answer, Call, Engine, Pulse, Rationale
from .events import CallEvent, CallStatus, Event, NoteEvent, PulseEvent, Role, Source, ThoughtEvent
from .facts import line_facts, parse_reply

PULSE_INTERVAL_S = 0.25
# Ids are unique per server process, so a ponder run's calls keep their ids when /api/think replays them.
_IDS = itertools.count(1)


@dataclass
class PulseGate:
    """Lets a pulse through at most once per interval."""

    interval_s: float = PULSE_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    last: float | None = field(default=None, init=False)

    def ready(self) -> bool:
        now = self.clock()
        if self.last is not None and now - self.last < self.interval_s:
            return False
        self.last = now
        return True


@dataclass(frozen=True)
class Spec:
    """Who a call is, for the browser: its role, a label like "Second opinion · most forcing", and its parent call."""

    role: Role
    label: str
    framing: str
    parent: str | None = None


MAIN = Spec("main", "Main", "plain")


@dataclass(frozen=True)
class Landed:
    key: str
    move: chess.Move
    answer: Answer
    expected: chess.Move | None  # the answer's expected reply, legal after `move`, else None


@dataclass(frozen=True)
class Decision:
    """The move to play, the call whose answer it is, and one sentence on why it won."""

    landed: Landed
    source: Source
    text: str
    chosen: tuple[str, ...]  # keys of the calls that answered this move and are marked chosen
    considered: tuple[chess.Move, ...] = ()  # other replies the analysis gave the opponent after this move


@dataclass
class _Live:
    id: str
    spec: Spec
    engine: str
    call: Call
    started: float
    gate: PulseGate


@dataclass(frozen=True)
class _Thought:
    key: str
    id: str
    text: str
    commentary: bool


@dataclass(frozen=True)
class _Pulse:
    key: str
    id: str


@dataclass(frozen=True)
class _Landed:
    id: str
    landed: Landed


@dataclass(frozen=True)
class _Failed:
    key: str
    id: str
    error: str
    answer: Answer | None = None


async def _run(engine: Engine, call: Call, by_san: dict[str, chess.Move], key: str, cid: str, queue: asyncio.Queue,
               record: bool = True) -> None:
    """Streams one engine call into the queue and, when `record`, records its latency when it lands."""
    started = time.monotonic()
    answer: Answer | None = None
    try:
        async with aclosing(engine.stream(call)) as events:
            async for event in events:
                if isinstance(event, Answer):
                    answer = event
                elif isinstance(event, Pulse):
                    queue.put_nowait(_Pulse(key, cid))
                else:
                    queue.put_nowait(_Thought(key, cid, event.text, isinstance(event, Rationale)))
        if answer is None:
            raise ValueError("the model returned no answer")
        move = by_san.get(answer.san)
        if move is None:
            raise ValueError(f"the model answered {answer.san!r}, which was not offered")
        if record:
            engine.latency.record(call.level, int((time.monotonic() - started) * 1000))
        after = chess.Board(call.fen)
        after.push(move)
        queue.put_nowait(_Landed(cid, Landed(key, move, answer, parse_reply(after, answer.expected_reply))))
    except Exception as e:
        queue.put_nowait(_Failed(key, cid, f"{type(e).__name__}: {e}", answer))


class Calls:
    """Every call for one move, by key. In a ponder run the main call becomes the `ponder` call, labelled with the
    reply it prepares for, and every other top-level call hangs under it."""

    def __init__(self, board: chess.Board, ponder: str | None = None):
        self.board = board
        self.ponder = ponder
        self.root: str | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.tasks: dict[str, asyncio.Task] = {}
        self.live: dict[str, _Live] = {}
        self.landed: dict[str, Landed] = {}
        self.failed: dict[str, str] = {}
        self.cancelled: set[str] = set()
        self.answers: list[Answer] = []
        self.engines: dict[str, Engine] = {}
        self.deadline_noted = False

    def start(self, key: str, engine: Engine, call: Call, by_san: dict[str, chess.Move], spec: Spec) -> str:
        if self.ponder is not None:
            if spec.role == "main":
                spec = replace(spec, role="ponder", label=f"If you play {self.ponder}" + spec.label.removeprefix("Main"))
            elif spec.parent is None:
                spec = replace(spec, parent=self.root)
        cid = f"c{next(_IDS)}"
        if spec.role == "ponder" and self.root is None:
            self.root = cid
        self.failed.pop(key, None)
        self.cancelled.discard(key)
        self.live[key] = _Live(cid, spec, engine.name, call, time.monotonic(), PulseGate())
        self.queue.put_nowait(self._event(key, "started"))
        # A second opinion is waited for only until the budget, so only its fast calls would land: recording them
        # would make a deep level look cheap enough to be the main call.
        record = spec.role != "second_opinion"
        self.engines[key] = engine
        self.tasks[key] = asyncio.create_task(_run(engine, call, by_san, key, cid, self.queue, record))
        return cid

    def id(self, key: str) -> str | None:
        """The call id under `key`, or None for a candidate that came from outside, as in the evaluation."""
        live = self.live.get(key)
        return live.id if live else None

    def resolved(self, key: str) -> bool:
        return key in self.landed or key in self.failed

    def pending(self, key: str) -> bool:
        return key in self.tasks and not self.resolved(key)

    def cancel(self, key: str, timed_out: bool = False) -> list[Event]:
        """Stops a pending call. `timed_out` records its elapsed time for its level as a lower bound, so slow calls cut
        off by the deadline still count in the latency table. A second opinion never records."""
        if not self.pending(key):
            return []
        live = self.live[key]
        if timed_out and live.spec.role != "second_opinion":
            self.engines[key].latency.record(live.call.level, int((time.monotonic() - live.started) * 1000))
        self.tasks[key].cancel()
        self.cancelled.add(key)
        self.failed[key] = "cancelled"
        return [self._event(key, "cancelled")]

    def cancel_pending(self) -> list[Event]:
        return [e for key in list(self.tasks) for e in self.cancel(key)]

    def mark(self, key: str, status: CallStatus) -> list[Event]:
        """`chosen` for a landed call."""
        return [self._event(key, status)] if key in self.landed else []

    def _event(self, key: str, status: CallStatus, error: str | None = None) -> CallEvent:
        live = self.live[key]
        s = live.spec
        out = CallEvent(live.id, status, s.role, s.label, s.parent, live.engine, live.call.level, s.framing, live.call.fen,
                        int((time.monotonic() - live.started) * 1000), error=error)
        landed = self.landed.get(key)
        if landed is None or status in ("started", "failed", "cancelled"):
            return out
        board = chess.Board(live.call.fen)
        san = board.san(landed.move)
        board.push(landed.move)
        expected = board.san(landed.expected) if landed.expected else None
        facts = line_facts(board, board.turn) if s.role == "reply" else ()
        return replace(out, move=san, uci=landed.move.uci(), expected_reply=expected, facts=facts)

    async def until(self, done: Callable[[], bool], deadline_at: float | None, note: bool = True) -> AsyncIterator[Event]:
        """Forwards call events, thoughts, pulses and notes until `done()`, nothing is pending, or `deadline_at` passes.
        `note` says "deadline reached" the first time the deadline passes; a wait that ends at the budget passes False."""
        while not done() and any(self.pending(k) for k in self.tasks):
            timeout = None if deadline_at is None else deadline_at - time.monotonic()
            try:
                if timeout is not None and timeout <= 0:
                    raise TimeoutError
                msg = await asyncio.wait_for(self.queue.get(), timeout)
            except TimeoutError:
                if note and not self.deadline_noted:
                    self.deadline_noted = True
                    yield NoteEvent("deadline reached")
                return
            for event in self._take(msg):
                yield event

    def _current(self, key: str, cid: str) -> bool:
        """False for a message from a cancelled call or from an earlier attempt under the same key."""
        return key not in self.cancelled and self.live[key].id == cid

    def _take(self, msg: CallEvent | _Thought | _Pulse | _Landed | _Failed) -> list[Event]:
        if isinstance(msg, CallEvent):
            return [msg]
        if isinstance(msg, _Pulse):
            ok = self._current(msg.key, msg.id) and self.live[msg.key].gate.ready()
            return [PulseEvent(msg.id)] if ok else []
        if isinstance(msg, _Thought):
            if not self._current(msg.key, msg.id):
                return []
            pulse = [PulseEvent(msg.id)] if self.live[msg.key].gate.ready() else []
            return pulse + [ThoughtEvent(msg.text, msg.id if msg.commentary else None, msg.id)]
        answer = msg.landed.answer if isinstance(msg, _Landed) else msg.answer
        if answer is not None:
            self.answers.append(answer)
        key = msg.landed.key if isinstance(msg, _Landed) else msg.key
        if not self._current(key, msg.id):
            return []
        if isinstance(msg, _Landed):
            self.landed[key] = msg.landed
            events: list[Event] = [self._event(key, "answered")]
            if key == "backup":
                events.append(NoteEvent(f"fallback ready: {self.board.san(msg.landed.move)}"))
            return events
        self.failed[key] = msg.error
        role = self.live[key].spec.role.replace("_", " ")
        return [self._event(key, "failed", msg.error), NoteEvent(f"{role} call failed: {msg.error}")]

    def close(self) -> list[asyncio.Task]:
        """Cancels unfinished calls and collects every answer still queued, synchronously."""
        for task in self.tasks.values():
            task.cancel()
        while not self.queue.empty():
            msg = self.queue.get_nowait()
            if isinstance(msg, _Landed):
                self.answers.append(msg.landed.answer)
            elif isinstance(msg, _Failed) and msg.answer is not None:
                self.answers.append(msg.answer)
        return list(self.tasks.values())
