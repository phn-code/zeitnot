import json
import re
from types import SimpleNamespace

import chess
import pytest

from zeitnot.engines import (Answer, Call, FakeEngine, GeminiEngine, LatencyTable, Rationale, RationaleParser, Thought,
                             parse_answer)
from zeitnot.facts import parse_reply

SQUARE = re.compile(r"(?<!\w)[a-h][1-8](?!\w)")
START = chess.Board().fen()


async def fake_run(choices, prompt="position", level="medium", fen=START):
    engine = FakeEngine({"low": 0, "medium": 0, "minimal": 0})
    return [e async for e in engine.stream(Call("system", prompt, tuple(choices), level, fen))]


def named(choices, text):
    return {c for c in choices if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", text)}


@pytest.mark.parametrize(
    "choices",
    [["Nf3", "e4", "Nc3", "d4", "a3", "h3"], ["Na3", "Nh3"], ["e4", "d4", "c4"]],
    ids=["mixed", "two choices", "pawn pushes only"],
)
async def test_fake_thoughts_name_the_answer_and_other_offered_moves(choices):
    events = await fake_run(choices)
    text = "".join(e.text for e in events if isinstance(e, Thought))
    answer = events[-1]
    assert isinstance(answer, Answer) and answer.san in choices
    assert answer.san in named(choices, text)
    assert len(named(choices, text)) >= min(3, len(choices))
    assert SQUARE.search(text)


async def test_fake_answer_expects_a_legal_reply():
    answer = (await fake_run(["e4", "d4", "Nf3"]))[-1]
    board = chess.Board()
    board.push_san(answer.san)
    assert parse_reply(board, answer.expected_reply) is not None and answer.note and answer.usd > 0


async def test_fake_is_deterministic_per_prompt():
    choices = ["Nf3", "e4", "Nc3", "d4", "a3", "h3", "c4"]
    assert await fake_run(choices, "same") == await fake_run(choices, "same")


def test_parse_answer_reads_json_and_truncates_the_note():
    san, note, expected = parse_answer('{"move": "Nf3", "expected_reply": " ...e5 ", "note": "' + " ".join(["w"] * 50) + '"}', True)
    assert san == "Nf3" and len(note.split()) == 40 and expected == "...e5"
    assert parse_answer('{"move": "Nf3", "expected_reply": "", "note": "x"}', True)[2] is None
    assert parse_answer(" e4 ", False) == ("e4", None, None)


COMMENTARY = 'Nxd5 \\"wins\\" a pawn?\\nNo: Nxf7 \\u2014 then Qf3.\\\\'


@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000], ids=lambda n: f"chunks of {n}")
def test_rationale_parser_decodes_the_commentary_progressively_at_any_chunk_boundary(size):
    raw = '{"commentary": "' + COMMENTARY + '", "move": "Nf3", "note": "Expect ...e5"}'
    parser = RationaleParser()
    pieces = [parser.feed(raw[i:i + size]) for i in range(0, len(raw), size)]
    assert "".join(pieces) == json.loads(raw)["commentary"]
    assert parser.closed
    if size == 1:
        assert sum(bool(p) for p in pieces) > 20, "text arrives while the string is still open"


def test_rationale_parser_stops_after_sixty_words_and_finds_the_key_anywhere():
    words = " ".join(f"w{i}" for i in range(80))
    raw = '{"move": "e4", "commentary" : "' + words + '"}'
    parser = RationaleParser()
    text = "".join(parser.feed(raw[i:i + 5]) for i in range(0, len(raw), 5))
    assert text.split() == [f"w{i}" for i in range(60)]


def test_rationale_parser_ignores_text_without_the_key():
    assert RationaleParser().feed('{"move": "e4", "note": "commentary"}') == ""


def test_parse_answer_reads_a_move_only_json_object():
    assert parse_answer('{"move":"O-O"}', False) == ("O-O", None, None)


async def test_fake_engine_streams_commentary_that_names_the_answer():
    events = await fake_run(["Nf3", "e4", "Nc3"])
    commentary = "".join(e.text for e in events if isinstance(e, Rationale))
    assert events[-1].san in commentary and len(commentary.split()) <= 60


class FakeGemini:
    """Stands in for genai.Client: streams the given answer text in three chunks after one thought part."""

    def __init__(self, text):
        self.text = text
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self.generate))
        self.config = None

    async def generate(self, model, contents, config):
        self.config = config
        part = lambda text, thought=False: SimpleNamespace(text=text, thought=thought)  # noqa: E731
        chunk = lambda *parts: SimpleNamespace(  # noqa: E731
            usage_metadata=SimpleNamespace(prompt_token_count=800, thoughts_token_count=50, candidates_token_count=20),
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))])

        async def chunks():
            yield chunk(part("**Looking**", thought=True))
            third = len(self.text) // 3
            for piece in (self.text[:third], self.text[third:2 * third], self.text[2 * third:]):
                yield chunk(part(piece))

        return chunks()


@pytest.mark.parametrize(("text", "with_note", "expected"), [
    ("Nf3", False, ("Nf3", None)),
    ('{"move": "Nf3"}', False, ("Nf3", None)),
    ('{"commentary": "Nf3 develops.", "move": "Nf3", "expected_reply": "e5", "note": "Centre."}', True, ("Nf3", "Centre.")),
], ids=["v1 enum text", "move-only json", "commentary, move, expected reply and note"])
async def test_gemini_parses_every_answer_shape(text, with_note, expected):
    client = FakeGemini(text)
    engine = GeminiEngine(client, "gemini-3.6-flash", LatencyTable({"low": (1, 1)}))
    events = [e async for e in engine.stream(Call("system", "prompt", ("Nf3", "e4"), "low", START, with_note))]
    assert (events[-1].san, events[-1].note) == expected and events[0] == Thought("**Looking**")
    assert events[-1].expected_reply == ("e5" if with_note else None)
    assert "".join(e.text for e in events if isinstance(e, Rationale)) == ("Nf3 develops." if with_note else "")
