"""The engine protocol, the live latency table, and the Gemini and fake engines. Codex lives in codex.py."""

import asyncio
import json
import random
import re
import zlib
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Protocol

import chess
from google import genai
from google.genai import types

from .config import GEMINI_PRICES, Level

NOTE_WORDS = 40
COMMENTARY_WORDS = 60
COMMENTARY_KEY = "commentary"


@dataclass(frozen=True)
class Thought:
    """A whole reasoning chunk, such as a Codex or Gemini thought summary."""

    text: str


@dataclass(frozen=True)
class Rationale:
    """New characters of the answer's commentary, decoded from the partial JSON as it streams."""

    text: str


@dataclass(frozen=True)
class Pulse:
    """The model is working but shows no text: Claude's empty thinking deltas, Codex reasoning and token usage."""


@dataclass(frozen=True)
class Answer:
    san: str
    note: str | None
    input_tokens: int
    thinking_tokens: int
    output_tokens: int
    usd: float = 0.0
    expected_reply: str | None = None  # SAN as the model wrote it; Calls validates it against the position


ModelEvent = Thought | Rationale | Pulse | Answer


@dataclass(frozen=True)
class Call:
    """One engine request. `with_note` asks for JSON {commentary, move, note}; otherwise the answer is the move alone."""

    system: str
    prompt: str
    choices: tuple[str, ...]
    level: Level
    fen: str
    with_note: bool = True


class LatencyTable:
    """p50, p90 and expected milliseconds per level: the seed until MIN_SAMPLES calls finish, then the last WINDOW
    calls. Latencies are skewed, with a long slow tail, so the mean the planner budgets for sits above the median."""

    MIN_SAMPLES = 3
    WINDOW = 20

    def __init__(self, seed: Mapping[Level, tuple[int, int]]):
        self.seed = dict(seed)
        self.samples: dict[Level, deque[int]] = {level: deque(maxlen=self.WINDOW) for level in seed}

    def record(self, level: Level, ms: int) -> None:
        self.samples.setdefault(level, deque(maxlen=self.WINDOW)).append(ms)

    def _quantile(self, level: Level, q: float, seeded: int) -> int:
        xs = sorted(self.samples.get(level, ()))
        if len(xs) < self.MIN_SAMPLES:
            return seeded
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    def p50(self, level: Level) -> int:
        return self._quantile(level, 0.5, self.seed[level][0])

    def p90(self, level: Level) -> int:
        return self._quantile(level, 0.9, self.seed[level][1])

    def expected(self, level: Level) -> int:
        """The mean of the recent calls, or the midpoint of the seed's p50 and p90 before there are enough."""
        xs = self.samples.get(level, ())
        if len(xs) < self.MIN_SAMPLES:
            return sum(self.seed[level]) // 2
        return sum(xs) // len(xs)

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {level: {"p50": self.p50(level), "p90": self.p90(level), "expected": self.expected(level),
                        "n": len(self.samples.get(level, ()))} for level in self.seed}


class Engine(Protocol):
    name: str
    model: str
    label: str
    paid: bool  # paid engines stop at the spend cap
    main_levels: tuple[Level, ...]  # cheapest first
    backup_level: Level  # the backup call runs only when the main call's level is above this one
    latency: LatencyTable

    def stream(self, call: Call) -> AsyncIterator[ModelEvent]: ...


def first_words(text: str, n: int) -> str:
    """`text` up to the end of its n-th word, whitespace kept, so a growing text keeps a growing prefix."""
    for i, m in enumerate(re.finditer(r"\S+", text)):
        if i == n - 1:
            return text[: m.end()]
    return text


def truncate_note(note: str | None) -> str | None:
    if note is None:
        return None
    words = note.split()
    return " ".join(words[:NOTE_WORDS]) or None


def expected_of(data: Mapping[str, Any]) -> str | None:
    return str(data.get("expected_reply") or "").strip() or None


def parse_answer(text: str, with_note: bool) -> tuple[str, str | None, str | None]:
    """The move, note and expected reply from the final answer text. A bare enum answer (Gemini without a note) is the
    move itself."""
    if not text.lstrip().startswith("{"):
        return text.strip(), None, None
    data = json.loads(text)
    if not with_note:
        return str(data["move"]).strip(), None, None
    return str(data["move"]).strip(), truncate_note(data.get("note")), expected_of(data)


def answer_schema(choices: Sequence[str], with_note: bool, commentary: bool | None = None) -> dict:
    """JSON Schema for Codex's outputSchema and Claude's --json-schema. Commentary comes first so it streams first.
    The expected reply cannot be an enum: its legal moves depend on the move, so Calls checks it after the answer."""
    props: dict[str, Any] = {}
    if with_note if commentary is None else commentary:
        props[COMMENTARY_KEY] = {"type": "string"}
    props["move"] = {"type": "string", "enum": list(choices)}
    if with_note:
        props["expected_reply"] = {"type": "string"}
        props["note"] = {"type": "string"}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_KEY = re.compile(r'"' + COMMENTARY_KEY + r'"\s*:\s*"')


class RationaleParser:
    """Feeds on the answer's raw JSON text as it streams and returns the commentary characters not yet returned.

    The string value is decoded up to the last complete character, so an escape split across chunks waits for the
    next chunk. Output stops after COMMENTARY_WORDS words.
    """

    def __init__(self) -> None:
        self.raw = ""
        self.sent = 0
        self.closed = False

    def feed(self, delta: str) -> str:
        if self.closed:
            return ""
        self.raw += delta
        start = _KEY.search(self.raw)
        if start is None:
            return ""
        text, self.closed = _decode_prefix(self.raw, start.end())
        text = first_words(text, COMMENTARY_WORDS)
        if len(text) <= self.sent:
            return ""
        new, self.sent = text[self.sent:], len(text)
        return new


def _decode_prefix(raw: str, i: int) -> tuple[str, bool]:
    """The JSON string starting at raw[i], decoded as far as it is complete, and whether its closing quote arrived."""
    out = []
    while i < len(raw):
        c = raw[i]
        if c == '"':
            return "".join(out), True
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= len(raw):
            break
        e = raw[i + 1]
        if e == "u":
            if i + 6 > len(raw):
                break
            out.append(chr(int(raw[i + 2:i + 6], 16)))
            i += 6
            continue
        out.append(_ESCAPES.get(e, e))
        i += 2
    return "".join(out), False


class GeminiEngine:
    name = "gemini"
    paid = True
    main_levels: tuple[Level, ...] = ("low", "medium")
    backup_level: Level = "minimal"

    def __init__(self, client: genai.Client, model: str, latency: LatencyTable):
        self.client = client
        self.model = model
        self.label = model.replace("-", " ").title()
        self.latency = latency

    async def stream(self, call: Call) -> AsyncIterator[ModelEvent]:
        choices = list(call.choices)
        if call.with_note:
            keys = [COMMENTARY_KEY, "move", "expected_reply", "note"]
            schema = {"type": "OBJECT", "properties": {k: {"type": "STRING", "enum": choices} if k == "move" else {"type": "STRING"}
                                                       for k in keys},
                      "required": keys, "propertyOrdering": keys}
            mime = "application/json"
        else:
            schema, mime = {"type": "STRING", "enum": choices}, "text/x.enum"
        config = types.GenerateContentConfig(
            system_instruction=call.system,
            thinking_config=types.ThinkingConfig(thinking_level=call.level, include_thoughts=True),
            response_mime_type=mime,
            response_schema=schema,
        )
        answer: list[str] = []
        rationale = RationaleParser()
        usage = None
        chunks = await self.client.aio.models.generate_content_stream(model=self.model, contents=call.prompt, config=config)
        async with aclosing(chunks):
            async for chunk in chunks:
                usage = chunk.usage_metadata or usage
                for cand in chunk.candidates or []:
                    for part in (cand.content.parts if cand.content else None) or []:
                        if not part.text:
                            continue
                        if part.thought:
                            yield Thought(part.text)
                            continue
                        answer.append(part.text)
                        if call.with_note and (new := rationale.feed(part.text)):
                            yield Rationale(new)
        san, note, expected = parse_answer("".join(answer), call.with_note)
        input_tokens = (usage.prompt_token_count or 0) if usage else 0
        thinking = (usage.thoughts_token_count or 0) if usage else 0
        output = (usage.candidates_token_count or 0) if usage else 0
        price = GEMINI_PRICES[self.model]
        yield Answer(san, note, input_tokens, thinking, output, price.cost(input_tokens, thinking + output), expected)


FAKE_SQUARES = ("d4", "e4", "d5", "e5", "c4", "f4", "c5", "f5")
FAKE_PRICE = GEMINI_PRICES["gemini-3.6-flash"]


def fake_thoughts(rng: random.Random, choices: list[str], san: str) -> tuple[str, ...]:
    """Three thoughts that name the answer, up to two other offered moves and one or two squares.

    A bare pawn push like "e4" reads as a square in the browser's gaze, so other moves are named first.
    """
    others = [c for c in choices if c != san]
    notation = [c for c in others if c[0].isupper() or "x" in c or "=" in c]
    named = rng.sample(notation if len(notation) >= 2 else others, min(2, len(others)))
    squares = " and ".join(rng.sample(FAKE_SQUARES, rng.choice((1, 2))))
    compared = f"{named[0]} and {named[1]} both keep" if len(named) == 2 else f"{named[0]} keeps" if named else "Most moves keep"
    return (
        f"**Scanning the position**\n\nLooking at captures, checks and threats for both sides. The fight is about {squares}.\n\n",
        f"**Comparing candidates**\n\n{compared} the position balanced, but {san} looks more active.\n\n",
        f"**Choosing a move**\n\n{san} develops without leaving a piece hanging.",
    )


def fake_commentary(rng: random.Random, choices: list[str], san: str) -> str:
    others = [c for c in choices if c != san]
    named = rng.sample(others, min(2, len(others)))
    weighed = f"I weighed {' and '.join(named)} against {san}." if named else f"{san} is the only real option."
    return f"{weighed} The fight is about {rng.choice(FAKE_SQUARES)}. {san} keeps every piece defended."


def fake_reply(fen: str, san: str, rng: random.Random) -> str | None:
    board = chess.Board(fen)
    board.push_san(san)
    replies = sorted(board.san(m) for m in board.legal_moves)
    return rng.choice(replies) if replies else None


class FakeEngine:
    """No API, no key. Scripted thoughts spread over the level's delay, then a deterministic choice per prompt.

    In fake mode it charges Gemini-like prices to exercise the spend cap. As the demo offered in real mode it is free.
    """

    name = "fake"
    model = "fake"
    paid = True
    label = "Fake Demo"
    main_levels: tuple[Level, ...] = ("low", "medium")
    backup_level: Level = "low"

    def __init__(self, delay_s: Mapping[Level, float], priced: bool = True):
        self.delay_s = dict(delay_s)
        self.paid = priced
        self.latency = LatencyTable({level: (int(s * 1000), int(s * 1000)) for level, s in self.delay_s.items()})

    async def stream(self, call: Call) -> AsyncIterator[ModelEvent]:
        seed = zlib.crc32(call.prompt.encode())
        choices = list(call.choices)
        san = choices[seed % len(choices)]
        rng = random.Random(seed)
        thoughts = fake_thoughts(rng, choices, san) if call.level != "minimal" else ()
        commentary = fake_commentary(rng, choices, san) if call.with_note else ""
        step = self.delay_s[call.level] / (len(thoughts) + 1)
        for text in thoughts:
            await asyncio.sleep(step)
            yield Pulse()
            yield Thought(text)
        pieces = re.findall(r"\S+\s*", commentary)
        for i in range(0, len(pieces), 3):
            await asyncio.sleep(step / (len(pieces) / 3 + 1))
            yield Rationale("".join(pieces[i:i + 3]))
        await asyncio.sleep(step / (len(pieces) / 3 + 1))
        expected = fake_reply(call.fen, san, rng) if call.with_note else None
        note = "Keep the pieces active and the king safe." if call.with_note else None
        tokens = (len(call.prompt) // 4, 100 * len(thoughts), 4 + len(pieces))
        yield Answer(san, note, *tokens, usd=FAKE_PRICE.cost(tokens[0], tokens[1] + tokens[2]) if self.paid else 0.0,
                     expected_reply=expected)


@dataclass
class EngineSet:
    """Every engine this server can use, by name. The server decides which are on offer right now."""

    engines: dict[str, Engine] = field(default_factory=dict)

    def __getitem__(self, name: str) -> Engine:
        return self.engines[name]

    def __contains__(self, name: str) -> bool:
        return name in self.engines
