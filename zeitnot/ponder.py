"""Pondering: the one-move pipeline run in the background on the position after a reply the model expects, cached by
engine and FEN, so one opponent never plays another's prepared answer. Each run belongs to a base position, the one
before the opponent's reply, so the replies considered on one turn ponder side by side.

A run in progress for a position /api/think now asks for is followed, not restarted. A request for another base
position, or /api/think for any other position, cancels the runs still in progress. Finished results stay cached
until evicted. /api/ponder/stream follows a run's events while the opponent thinks.
"""

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Literal

from .events import CallEvent, DecisionEvent, ErrorEvent, Event, MoveEvent, PulseEvent

Status = Literal["started", "running", "ready"]


class PonderRun:
    def __init__(self, engine: str, fen: str, base: str, events: AsyncIterator[Event]):
        self.engine = engine
        self.fen = fen
        self.base = base
        self.buffer: list[Event] = []
        self.result: MoveEvent | None = None
        self.decision: DecisionEvent | None = None
        self.done = False
        self.followers: list[asyncio.Queue] = []
        self.task = asyncio.create_task(self._drive(events))

    async def _drive(self, events: AsyncIterator[Event]) -> None:
        try:
            async with aclosing(events) as stream:
                async for event in stream:
                    if isinstance(event, MoveEvent):
                        self.result = event
                        continue
                    if isinstance(event, ErrorEvent):
                        continue
                    if isinstance(event, DecisionEvent):
                        self.decision = event
                    if not isinstance(event, PulseEvent):
                        self.buffer.append(event)
                    for q in self.followers:
                        q.put_nowait(event)
        finally:
            self.done = True
            for q in self.followers:
                q.put_nowait(None)

    async def follow(self) -> AsyncIterator[Event]:
        """Every event so far except pulses, then live ones until the run ends. The run's move is read from `result`."""
        replay = list(self.buffer)
        if self.done:
            for event in replay:
                yield event
            return
        q: asyncio.Queue = asyncio.Queue()
        self.followers.append(q)
        try:
            for event in replay:
                yield event
            while (event := await q.get()) is not None:
                yield event
        finally:
            self.followers.remove(q)

    def answered(self) -> list[str]:
        """UCI moves the run's calls answered in its own position: what the model prepared for this reply."""
        return [e.uci for e in self.buffer if isinstance(e, CallEvent) and e.status == "answered" and e.fen == self.fen and e.uci]

    def cancel(self) -> None:
        if not self.done:
            self.task.cancel()


class Ponderer:
    def __init__(self, size: int = 32):
        self.size = size
        self.runs: OrderedDict[tuple[str, str], PonderRun] = OrderedDict()  # by (engine, FEN)

    def _cancel_running(self, keep: Callable[[PonderRun], bool]) -> None:
        for key, run in list(self.runs.items()):
            if not run.done and not keep(run):
                run.cancel()
                del self.runs[key]

    def keep_base(self, engine: str, base: str) -> None:
        """Cancels runs in progress that ponder another turn or for another engine."""
        self._cancel_running(lambda run: run.engine == engine and run.base == base)

    def start(self, engine: str, fen: str, base: str, pipeline: Callable[[], AsyncIterator[Event]]) -> Status:
        """Starts pondering `fen` for `engine` unless it is already running or cached. `base` is the position before
        the reply."""
        self.keep_base(engine, base)
        run = self.runs.get((engine, fen))
        if run is not None:
            return "ready" if run.done else "running"
        self.runs[(engine, fen)] = PonderRun(engine, fen, base, pipeline())
        while len(self.runs) > self.size:
            _, old = self.runs.popitem(last=False)
            old.cancel()
        return "started"

    def get(self, engine: str, fen: str) -> PonderRun | None:
        return self.runs.get((engine, fen))

    def prepared(self, engine: str, base: str, fen: str) -> tuple[str, ...]:
        """Moves this engine's runs of this turn prepared for replies other than the one that reached `fen`."""
        moves = [m for run in self.runs.values() if run.engine == engine and run.base == base and run.fen != fen
                 for m in run.answered()]
        return tuple(dict.fromkeys(moves))

    def claim(self, engine: str, fen: str) -> PonderRun | None:
        """The run for the position /api/think is about to play, after cancelling every other run in progress."""
        self._cancel_running(lambda run: run.engine == engine and run.fen == fen)
        return self.runs.get((engine, fen))

    async def close(self) -> None:
        for run in self.runs.values():
            run.cancel()
        await asyncio.gather(*(r.task for r in self.runs.values()), return_exceptions=True)
        self.runs.clear()
