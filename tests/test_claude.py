import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zeitnot import claude
from zeitnot.timeman import INSTANT_PRIOR, Clock, plan, second_level
from zeitnot.claude import ClaudeEngine, ClaudeError, claude_available
from zeitnot.engines import Answer, Call, Pulse, Rationale

FAKE = Path(__file__).parent / "fake_claude.py"
CHOICES = ("e4", "d4", "Nf3", "c4")
START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-the-cli")
    return path


@pytest.fixture
def workdir(tmp_path):
    path = tmp_path / "empty"
    path.mkdir()
    return path


def runs(log_path):
    return [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []


def engine(workdir, bare=False):
    return ClaudeEngine(workdir, bare=bare, command=[sys.executable, str(FAKE)])


def call(prompt, level="low", with_note=True):
    return Call("You are a chess player.", prompt, CHOICES, level, START, with_note)


async def collect(e, c):
    return [x async for x in e.stream(c)]


def flag(argv, name):
    return argv[argv.index(name) + 1]


async def test_a_call_streams_commentary_and_pulses_then_answers_from_structured_output(log_path, workdir):
    events = await collect(engine(workdir), call("move=Nf3 think", level="high"))
    commentary = "".join(e.text for e in events if isinstance(e, Rationale))
    assert commentary == 'Comparing Nf3 with "d4": both fight for é5.\nThe knight on g5 eyes f7.'
    assert sum(isinstance(e, Rationale) for e in events) > 3, "commentary arrives in pieces, not at the end"
    assert events.index(next(e for e in events if isinstance(e, Pulse))) < 2, "thinking deltas pulse before any text"
    assert events[-1] == Answer("Nf3", "Develop and castle.", 2202, 20, 200, expected_reply="...e5")

    [run] = runs(log_path)
    argv = run["argv"]
    assert run["cwd"] == str(workdir) and run["api_key"] is False
    assert (flag(argv, "--model"), flag(argv, "--effort"), flag(argv, "--tools"), flag(argv, "--setting-sources")) == ("opus", "high", "", "")
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(argv) and "--bare" not in argv
    schema = json.loads(flag(argv, "--json-schema"))
    assert list(schema["properties"]) == ["commentary", "move", "expected_reply", "note"] and schema["properties"]["move"]["enum"] == list(CHOICES)


async def test_a_refused_answer_is_asked_once_more_without_commentary(log_path, workdir):
    events = await collect(engine(workdir), call("refuse move=c4"))
    assert events[-1].san == "c4" and not any(isinstance(e, Rationale) for e in events)
    first, second = (json.loads(flag(r["argv"], "--json-schema")) for r in runs(log_path))
    assert "commentary" in first["properties"] and list(second["properties"]) == ["move", "expected_reply", "note"]


async def test_closing_the_stream_kills_the_process(log_path, workdir):
    stream = engine(workdir).stream(call("move=e4 delay=30"))
    assert isinstance(await stream.__anext__(), Pulse)
    await stream.aclose()
    with pytest.raises(ProcessLookupError):
        os.kill(runs(log_path)[0]["pid"], 0)


async def test_cancelling_the_task_kills_the_process(log_path, workdir):
    task = asyncio.create_task(collect(engine(workdir), call("move=e4 delay=30")))
    while not runs(log_path):
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(ProcessLookupError):
        os.kill(runs(log_path)[0]["pid"], 0)


async def test_a_crash_raises_with_the_cli_message(log_path, workdir):
    with pytest.raises(ClaudeError, match="Please run /login"):
        await collect(engine(workdir), call("crash"))


async def test_bare_mode_passes_bare_and_keeps_the_api_key(log_path, workdir):
    await collect(engine(workdir, bare=True), call("move=d4"))
    [run] = runs(log_path)
    assert "--bare" in run["argv"] and run["api_key"] is True


async def test_a_move_only_call_has_no_commentary_or_note(log_path, workdir):
    events = await collect(engine(workdir), call("move=d4", with_note=False))
    assert events[-1].san == "d4" and events[-1].note is None
    assert list(json.loads(flag(runs(log_path)[0]["argv"], "--json-schema"))["properties"]) == ["move"]


def status(stdout, code=0):
    def run(argv, **kw):
        assert argv == ["claude", "auth", "status", "--json"] and "ANTHROPIC_API_KEY" not in kw["env"]
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")
    return run


@pytest.mark.parametrize(("stdout", "code", "expected"), [
    ('{"loggedIn": true, "authMethod": "claude.ai"}', 0, True),
    ('{"loggedIn": false, "authMethod": "none"}', 1, False),
    ("not json", 0, False),
], ids=["logged in", "logged out", "garbled"])
def test_availability_reads_auth_status(monkeypatch, stdout, code, expected):
    monkeypatch.setattr(claude.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert claude_available(False, status(stdout, code)) is expected


def test_availability_without_the_cli_or_with_a_key_in_bare_mode(monkeypatch):
    monkeypatch.setattr(claude.shutil, "which", lambda name: None)
    assert claude_available(False) is False
    monkeypatch.setattr(claude.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert claude_available(True) is True


@pytest.mark.parametrize("level", ["xhigh", "max"])
async def test_the_top_levels_pass_their_own_effort_flag(log_path, workdir, level):
    await collect(engine(workdir), call("move=e4", level=level))
    assert flag(runs(log_path)[0]["argv"], "--effort") == level


def test_opus_levels_per_clock_with_the_starting_ponder_rate():
    """Normal moves at 5+3 stay at low: high's expected 29 s, not its 18 s median, is what the budget pays. xhigh is
    the main call only at 15+10; from 5+3 it is the hard move's second opinion, covered by the main answer."""
    e = ClaudeEngine(Path("."))
    assert (e.latency.p50("xhigh"), e.latency.p90("max")) == (20_100, 600_000)
    assert e.main_levels[-1] == "xhigh" and "max" not in e.main_levels

    def levels(minutes, increment):
        clock = Clock(minutes * 60_000, minutes * 60_000, increment * 1000)
        out = []
        for tier in ("easy", "normal", "hard"):
            p = plan(clock, 20, tier, e.latency, e.main_levels, INSTANT_PRIOR)
            second = second_level(p.level, p.budget_ms, e.latency, e.main_levels) if tier == "hard" else p.level
            out.append(p.level if second == p.level else f"{p.level}+{second}")
        return out

    assert levels(3, 2) == ["low", "low", "medium+high"]
    assert levels(5, 3) == ["low", "low", "high+xhigh"]
    assert levels(10, 5) == ["medium", "high", "high+xhigh"]
    assert levels(15, 10) == ["high", "high", "xhigh"]
