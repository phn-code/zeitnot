"""What a model client yields while streaming, the client protocol, and the two clients."""

import asyncio
import random
import zlib
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Protocol

from google import genai
from google.genai import types

from .config import Level
from .rules import INSTRUCTIONS


@dataclass(frozen=True)
class Thought:
    text: str


@dataclass(frozen=True)
class Answer:
    san: str
    input_tokens: int
    thinking_tokens: int
    output_tokens: int


ModelEvent = Thought | Answer


class ModelClient(Protocol):
    def stream(self, prompt: str, choices: list[str], level: Level) -> AsyncIterator[ModelEvent]: ...


class GeminiClient:
    def __init__(self, client: genai.Client, model: str):
        self.client = client
        self.model = model

    async def stream(self, prompt: str, choices: list[str], level: Level) -> AsyncIterator[ModelEvent]:
        config = types.GenerateContentConfig(
            system_instruction=INSTRUCTIONS,
            thinking_config=types.ThinkingConfig(thinking_level=level, include_thoughts=True),
            response_mime_type="text/x.enum",
            response_schema={"type": "STRING", "enum": choices},
        )
        answer: list[str] = []
        usage = None
        chunks = await self.client.aio.models.generate_content_stream(model=self.model, contents=prompt, config=config)
        async with aclosing(chunks):
            async for chunk in chunks:
                usage = chunk.usage_metadata or usage
                for cand in chunk.candidates or []:
                    for part in (cand.content.parts if cand.content else None) or []:
                        if not part.text:
                            continue
                        if part.thought:
                            yield Thought(part.text)
                        else:
                            answer.append(part.text)
        yield Answer(
            san="".join(answer).strip(),
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            thinking_tokens=(usage.thoughts_token_count or 0) if usage else 0,
            output_tokens=(usage.candidates_token_count or 0) if usage else 0,
        )


FAKE_SQUARES = ("d4", "e4", "d5", "e5", "c4", "f4", "c5", "f5")


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


class FakeClient:
    """No API key. Scripted thoughts spread over the level's delay, then a deterministic choice."""

    def __init__(self, delay_s: Mapping[Level, float]):
        self.delay_s = delay_s

    async def stream(self, prompt: str, choices: list[str], level: Level) -> AsyncIterator[ModelEvent]:
        seed = zlib.crc32(prompt.encode())
        san = choices[seed % len(choices)]
        thoughts = fake_thoughts(random.Random(seed), choices, san) if level != "minimal" else ()
        step = self.delay_s[level] / (len(thoughts) + 1)
        for text in thoughts:
            await asyncio.sleep(step)
            yield Thought(text)
        await asyncio.sleep(step)
        yield Answer(san=san, input_tokens=len(prompt) // 4, thinking_tokens=100 * len(thoughts), output_tokens=4)
