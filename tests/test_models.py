import re

import pytest

from zeitnot.models import Answer, FakeClient, Thought

SQUARE = re.compile(r"(?<!\w)[a-h][1-8](?!\w)")


async def fake_run(choices, prompt="position"):
    client = FakeClient({"medium": 0, "minimal": 0})
    return [e async for e in client.stream(prompt, choices, "medium")]


def named(choices, text):
    return {c for c in choices if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", text)}


@pytest.mark.parametrize(
    "choices",
    [["Nf3", "e4", "Bc4", "d4", "Qh5", "O-O"], ["Kh1", "Kg2"], ["e4", "d4", "c4"], ["exd5", "Nxe5", "Bb5+", "a3"]],
    ids=["mixed", "two choices", "pawn pushes only", "captures and checks"],
)
async def test_fake_thoughts_name_the_answer_and_other_offered_moves(choices):
    events = await fake_run(choices)
    text = "".join(e.text for e in events if isinstance(e, Thought))
    answer = events[-1]
    assert isinstance(answer, Answer) and answer.san in choices
    assert answer.san in named(choices, text)
    assert len(named(choices, text)) >= min(3, len(choices))
    assert SQUARE.search(text)


async def test_fake_thoughts_are_deterministic_per_prompt():
    choices = ["Nf3", "e4", "Bc4", "d4", "Qh5", "Nc3", "Be2"]
    assert await fake_run(choices, "same") == await fake_run(choices, "same")


async def test_fake_fallback_call_streams_no_thoughts():
    client = FakeClient({"minimal": 0})
    events = [e async for e in client.stream("p", ["e4", "d4"], "minimal")]
    assert len(events) == 1 and isinstance(events[0], Answer)
