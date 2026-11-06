"""Orchestrates one move: plan, fallback task, main task, deadline, commit. Also the NDJSON event types."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import chess

from . import timeman
from .config import PRICES, Level, Settings
from .ledger import SpendLedger
from .models import Answer, ModelClient, Thought
from .rules import board_from_moves, render, safe_moves
from .timeman import Clock

Source = Literal["model", "fallback", "forced", "emergency"]


@dataclass(frozen=True)
class ThinkRequest:
    moves: tuple[str, ...]
    clock: Clock
    model: str


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
    type: Literal["plan"] = field(default="plan", init=False)


@dataclass(frozen=True)
class NoteEvent:
    text: str
    type: Literal["note"] = field(default="note", init=False)


@dataclass(frozen=True)
class ThoughtEvent:
    text: str
    type: Literal["thought"] = field(default="thought", init=False)


@dataclass(frozen=True)
class MoveEvent:
    uci: str
    san: str
    source: Source
    elapsed_ms: int
    usd: float
    tokens: Tokens
    type: Literal["move"] = field(default="move", init=False)


@dataclass(frozen=True)
class ErrorEvent:
    message: str
    type: Literal["error"] = field(default="error", init=False)


Event = PlanEvent | NoteEvent | ThoughtEvent | MoveEvent | ErrorEvent


def to_ndjson(event: Event) -> str:
    return json.dumps(asdict(event)) + "\n"


@dataclass(frozen=True)
class _Landed:
    source: Source
    move: chess.Move
    answer: Answer


@dataclass(frozen=True)
class _Failed:
    source: Source
    error: str
    answer: Answer | None = None


_LABEL: dict[Source, str] = {"model": "main", "fallback": "fallback"}


async def _run(
    client: ModelClient,
    prompt: str,
    by_san: dict[str, chess.Move],
    level: Level,
    source: Source,
    queue: "asyncio.Queue[Thought | _Landed | _Failed]",
) -> None:
    """Streams one model call into the queue. Only the main call forwards thoughts."""
    answer: Answer | None = None
    try:
        async with aclosing(client.stream(prompt, list(by_san), level)) as events:
            async for event in events:
                if isinstance(event, Answer):
                    answer = event
                elif source == "model":
                    await queue.put(event)
        if answer is None:
            raise ValueError("the model returned no answer")
        move = by_san.get(answer.san)
        if move is None:
            raise ValueError(f"the model answered {answer.san!r}, which was not offered")
        queue.put_nowait(_Landed(source, move, answer))
    except Exception as e:
        queue.put_nowait(_Failed(source, f"{type(e).__name__}: {e}", answer))


def _cost_usd(model: str, answers: list[Answer]) -> float:
    price = PRICES[model]
    return sum(price.cost(a.input_tokens, a.thinking_tokens + a.output_tokens) for a in answers)


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


async def think(
    req: ThinkRequest, client: ModelClient, ledger: SpendLedger, settings: Settings
) -> AsyncIterator[Event]:
    started = time.monotonic()
    board = board_from_moves(req.moves)
    legal = list(board.legal_moves)

    if not legal:
        yield ErrorEvent("the game is over, there is no move to make")
        return
    if len(legal) == 1:
        yield MoveEvent(legal[0].uci(), board.san(legal[0]), "forced", 0, 0.0, Tokens())
        return
    if ledger.spent_usd >= settings.cap_usd:
        yield ErrorEvent(f"spend cap reached: ${ledger.spent_usd:.2f} of ${settings.cap_usd:.2f}")
        return

    plan = timeman.plan(req.clock, len(board.move_stack), len(legal), settings.latency_p50_ms)
    yield PlanEvent(plan.budget_ms, plan.deadline_ms, plan.level, plan.reason)

    queue: asyncio.Queue[Thought | _Landed | _Failed] = asyncio.Queue()
    tasks: list[asyncio.Task] = []
    answers: list[Answer] = []
    landed: dict[Source, chess.Move] = {}
    running: set[Source] = set()
    try:
        if plan.deadline_ms > 0:
            safe = {board.san(m): m for m in safe_moves(board)}
            fallback_prompt = render(board, list(safe), "Candidate moves")
            tasks.append(asyncio.create_task(_run(client, fallback_prompt, safe, "minimal", "fallback", queue)))
            running.add("fallback")
            if plan.level != "none":
                every = {board.san(m): m for m in legal}
                main_prompt = render(board, list(every))
                tasks.append(asyncio.create_task(_run(client, main_prompt, every, plan.level, "model", queue)))
                running.add("model")
        else:
            yield NoteEvent("no time for an API call, playing a rules-only move")

        deadline_at = started + plan.deadline_ms / 1000
        while "model" not in landed and running:
            remaining = deadline_at - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                msg = await asyncio.wait_for(queue.get(), remaining)
            except TimeoutError:
                yield NoteEvent("deadline reached")
                break
            if isinstance(msg, Thought):
                yield ThoughtEvent(msg.text)
            elif isinstance(msg, _Landed):
                answers.append(msg.answer)
                landed[msg.source] = msg.move
                running.discard(msg.source)
                if msg.source == "fallback":
                    yield NoteEvent(f"fallback ready: {board.san(msg.move)}")
            else:
                if msg.answer is not None:
                    answers.append(msg.answer)
                running.discard(msg.source)
                yield NoteEvent(f"{_LABEL[msg.source]} call failed: {msg.error}")

        if "model" in landed:
            move, source = landed["model"], "model"
        elif "fallback" in landed:
            move, source = landed["fallback"], "fallback"
            yield NoteEvent(f"playing fallback move {board.san(move)}")
        else:
            move, source = safe_moves(board)[0], "emergency"
            yield NoteEvent(f"no model move available, playing emergency move {board.san(move)}")
        elapsed_ms = int((time.monotonic() - started) * 1000)
    finally:
        for task in tasks:
            task.cancel()
        while not queue.empty():
            msg = queue.get_nowait()
            if isinstance(msg, _Landed) or (isinstance(msg, _Failed) and msg.answer is not None):
                answers.append(msg.answer)
        usd = _cost_usd(req.model, answers)
        ledger.add(usd)
        # Bookkeeping above is synchronous on purpose: when the client disconnects, the server cancels this
        # coroutine and every later await in the finally is cancelled again.
        await asyncio.gather(*tasks, return_exceptions=True)

    tokens = _tokens(answers)
    _append_log(
        settings.log_path,
        {
            "ts": time.time(),
            "model": req.model,
            "moves": list(req.moves),
            "plan": asdict(plan),
            "source": source,
            "uci": move.uci(),
            "san": board.san(move),
            "elapsed_ms": elapsed_ms,
            "usd": usd,
            "tokens": asdict(tokens),
            "model_san": board.san(landed["model"]) if "model" in landed else None,
            "fallback_san": board.san(landed["fallback"]) if "fallback" in landed else None,
        },
    )
    yield MoveEvent(move.uci(), board.san(move), source, elapsed_ms, usd, tokens)
