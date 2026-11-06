import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from zeitnot.codex import AppServer, AppServerError, CodexEngine, app_server_argv, mcp_server_names
from zeitnot.engines import Answer, Call, Pulse, Rationale, Thought

FAKE = Path(__file__).parent / "fake_codex.py"
CHOICES = ("e4", "d4", "Nf3", "c4")


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "codex.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(path))
    return path


@pytest.fixture
async def server(log_path):
    s = AppServer([sys.executable, str(FAKE)])
    yield s
    await s.close()


def received(log_path):
    return [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []


def call(prompt, level="low", with_note=True):
    return Call("You are a chess player.", prompt, CHOICES, level, "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", with_note)


async def collect(engine, c):
    return [e async for e in engine.stream(c)]


async def test_one_call_streams_summary_parts_and_answers_json_with_usage(server, log_path):
    engine = CodexEngine(server, "gpt-6.1-sol")
    events = await collect(engine, call("move=Nf3"))
    assert [e for e in events if isinstance(e, Thought)] == [Thought("**Looking around**\n\n"), Thought("Thinking about Nf3.")]
    assert "".join(e.text for e in events if isinstance(e, Rationale)) == 'Weighing Nf3 against "d4".\nBoth hold e5.'
    assert any(isinstance(e, Pulse) for e in events)
    assert events[-1] == Answer("Nf3", "Develop and castle.", 900, 120, 30, 0.0, "e5")
    methods = [m.get("method") for m in received(log_path)]
    assert methods[:4] == ["initialize", "initialized", "thread/start", "turn/start"]
    start = next(m for m in received(log_path) if m.get("method") == "thread/start")["params"]
    assert start == {"ephemeral": True, "model": "gpt-6.1-sol", "baseInstructions": "You are a chess player.",
                     "sandbox": "read-only", "approvalPolicy": "never"}
    turn = next(m for m in received(log_path) if m.get("method") == "turn/start")["params"]
    assert turn["effort"] == "low" and turn["summary"] == "auto"
    assert turn["outputSchema"]["properties"]["move"]["enum"] == list(CHOICES)
    assert turn["outputSchema"]["required"] == ["commentary", "move", "expected_reply", "note"] and turn["outputSchema"]["additionalProperties"] is False
    await asyncio.sleep(0.05)
    assert "thread/unsubscribe" in [m.get("method") for m in received(log_path)]


async def test_concurrent_calls_share_one_process_and_route_by_thread(server):
    engine = CodexEngine(server, "gpt-6.1-sol")
    slow, fast, mid = await asyncio.gather(
        collect(engine, call("move=c4 delay=0.4")), collect(engine, call("move=d4 delay=0.05")), collect(engine, call("move=Nf3 delay=0.2")),
    )
    assert [e.san for e in (slow[-1], fast[-1], mid[-1])] == ["c4", "d4", "Nf3"]
    thoughts = [[e for e in r if isinstance(e, Thought)] for r in (slow, fast)]
    assert thoughts[0][1] == Thought("Thinking about c4.") and thoughts[1][1] == Thought("Thinking about d4.")
    assert server.starts == 1


async def test_closing_the_stream_sends_turn_interrupt(server, log_path):
    engine = CodexEngine(server, "gpt-6.1-sol")
    stream = engine.stream(call("move=e4 delay=5"))
    first = await stream.__anext__()
    assert isinstance(first, Pulse)
    await stream.aclose()
    await asyncio.sleep(0.1)
    interrupt = [m for m in received(log_path) if m.get("method") == "turn/interrupt"]
    assert len(interrupt) == 1 and interrupt[0]["params"] == {"threadId": "t1", "turnId": "u1"}


async def test_cancelling_the_task_mid_call_sends_turn_interrupt(server, log_path):
    engine = CodexEngine(server, "gpt-6.1-sol")
    task = asyncio.create_task(collect(engine, call("move=e4 delay=5")))
    await asyncio.sleep(0.4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.1)
    assert [m["params"]["turnId"] for m in received(log_path) if m.get("method") == "turn/interrupt"] == ["u1"]


async def test_a_dead_process_fails_the_call_and_the_next_call_restarts_it(server):
    engine = CodexEngine(server, "gpt-6.1-sol")
    with pytest.raises(AppServerError):
        await collect(engine, call("die"))
    events = await collect(engine, call("move=d4"))
    assert events[-1].san == "d4" and server.starts == 2


async def test_a_failed_turn_raises_with_its_message(server):
    with pytest.raises(AppServerError, match="usage limit"):
        await collect(CodexEngine(server, "gpt-6.1-sol"), call("fail"))


async def test_approval_requests_are_declined(server, log_path):
    events = await collect(CodexEngine(server, "gpt-6.1-sol"), call("approve move=c4"))
    assert events[-1].san == "c4"
    reply = next(m for m in received(log_path) if m.get("id") == 9001 and "method" not in m)
    assert reply["result"] == {"decision": "decline"}


async def test_service_tier_is_sent_per_turn_when_set(server, log_path):
    await collect(CodexEngine(server, "gpt-6.1-sol", service_tier="fast"), call("move=e4"))
    turn = next(m for m in received(log_path) if m.get("method") == "turn/start")["params"]
    assert turn["serviceTierForTurn"] == "fast"


def completed(stdout, code=0):
    return lambda *a, **k: subprocess.CompletedProcess(a[0], code, stdout=stdout, stderr="")


def test_mcp_servers_are_discovered_by_name_and_each_disabled():
    listing = json.dumps([{"name": "langsmith", "enabled": True, "transport": {"env": {"SECRET": "x"}}},
                          {"name": "playwright", "enabled": True}])
    names = mcp_server_names(completed(listing))
    assert names == ["langsmith", "playwright"]
    argv = app_server_argv(names)
    assert argv[:2] == ["codex", "app-server"]
    pairs = list(zip(argv, argv[1:]))
    assert ("--disable", "plugins") in pairs
    assert ("-c", "mcp_servers.langsmith.enabled=false") in pairs and ("-c", "mcp_servers.playwright.enabled=false") in pairs
    assert not any(a.startswith("mcp_servers=") for a in argv)


def test_a_failed_mcp_listing_warns_and_keeps_plugins_disabled(caplog):
    assert mcp_server_names(completed("", code=1)) == []
    assert mcp_server_names(completed("not json")) == []
    assert "could not list Codex MCP servers" in caplog.text
    argv = app_server_argv([])
    assert ("--disable", "plugins") in list(zip(argv, argv[1:]))
    assert not any("mcp_servers" in a for a in argv)


async def test_a_move_only_answer_is_parsed_out_of_its_json(server, log_path):
    events = await collect(CodexEngine(server, "gpt-6.1-sol"), call("move=c4", with_note=False))
    assert events[-1] == Answer("c4", None, 900, 120, 30, 0.0)
    turn = next(m for m in received(log_path) if m.get("method") == "turn/start")["params"]
    assert list(turn["outputSchema"]["properties"]) == ["move"]
    assert not any(isinstance(e, Rationale) for e in events)


@pytest.mark.parametrize("level", ["xhigh", "max"])
async def test_the_top_levels_are_sent_as_the_turn_effort(server, log_path, level):
    engine = CodexEngine(server, "gpt-6.1-sol")
    await collect(engine, call("move=e4", level=level))
    turn = next(m for m in received(log_path) if m.get("method") == "turn/start")["params"]
    assert turn["effort"] == level
    assert engine.latency.p50(level) > engine.latency.p50("medium") and level not in engine.main_levels
