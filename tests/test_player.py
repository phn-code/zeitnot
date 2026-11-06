import asyncio
import json

import chess

from zeitnot.config import PRICES
from zeitnot.ledger import SpendLedger
from zeitnot.player import ErrorEvent, MoveEvent, NoteEvent, PlanEvent, ThoughtEvent, ThinkRequest, think
from zeitnot.rules import board_from_moves, emergency_move
from zeitnot.timeman import Clock

from conftest import FORCED_HISTORY, SHORT_DEADLINE_CLOCK, Script, ScriptedClient, run_think

PRICE = PRICES["gemini-3.6-flash"]


def usd(*calls):
    return sum(PRICE.cost(i, t + o) for i, t, o in calls)


def notes(events):
    return [e.text for e in events if isinstance(e, NoteEvent)]


def final(events):
    assert isinstance(events[-1], MoveEvent)
    assert sum(isinstance(e, MoveEvent) for e in events) == 1
    return events[-1]


async def test_forced_move_skips_plan_and_model(settings, ledger):
    client = ScriptedClient()
    events = await run_think(client, ledger, settings, moves=FORCED_HISTORY)
    assert [type(e) for e in events] == [MoveEvent]
    assert (events[0].san, events[0].source, events[0].usd) == ("exd6", "forced", 0.0)
    assert client.log == []


async def test_main_answer_in_time_is_played_and_costed(settings, ledger):
    client = ScriptedClient(
        main=Script(san="e4", delay_s=0.2, thoughts=("**Plan**", " go")),
        fallback=Script(san="Nf3", delay_s=0.01, tokens=(500, 0, 5)),
    )
    events = await run_think(client, ledger, settings)
    move = final(events)
    assert isinstance(events[0], PlanEvent) and events[0].level == "medium" and events[0].deadline_ms == 3_000
    assert [e.text for e in events if isinstance(e, ThoughtEvent)] == ["**Plan**", " go"]
    assert "fallback ready: Nf3" in notes(events)
    assert (move.uci, move.san, move.source) == ("e2e4", "e4", "model")
    assert move.tokens.input == 1500 and move.tokens.thinking == 2000 and move.tokens.output == 15
    assert move.usd == usd((1000, 2000, 10), (500, 0, 5))
    assert ledger.spent_usd == move.usd
    assert json.loads(settings.ledger_path.read_text())["spent_usd"] == move.usd
    assert client.levels_called == ["minimal", "medium"]


async def test_unfinished_fallback_is_cancelled_and_not_charged(settings, ledger):
    client = ScriptedClient(main=Script(delay_s=0.05), fallback=Script(san="Nf3", delay_s=5))
    events = await run_think(client, ledger, settings)
    move = final(events)
    assert move.source == "model"
    assert "cancelled:minimal" in client.log
    assert move.usd == usd((1000, 2000, 10))


async def test_deadline_reached_commits_fallback_and_cancels_main(settings, ledger):
    client = ScriptedClient(main=Script(delay_s=5, thoughts=("thinking",)), fallback=Script(san="Nf3", delay_s=0.01, tokens=(500, 0, 5)))
    events = await run_think(client, ledger, settings)
    move = final(events)
    assert (move.san, move.source) == ("Nf3", "fallback")
    assert "deadline reached" in notes(events)
    assert 2_950 <= move.elapsed_ms < 4_500
    assert "cancelled:medium" in client.log
    assert move.usd == usd((500, 0, 5))


async def test_main_error_commits_fallback_without_waiting_for_deadline(settings, ledger):
    client = ScriptedClient(
        main=Script(delay_s=0.01, error=RuntimeError("503 high demand")), fallback=Script(san="Nf3", delay_s=0.1)
    )
    events = await run_think(client, ledger, settings)
    move = final(events)
    assert (move.san, move.source) == ("Nf3", "fallback")
    assert any("main call failed" in n and "503 high demand" in n for n in notes(events))
    assert "deadline reached" not in notes(events)
    assert move.elapsed_ms < 600


async def test_main_answer_not_among_offered_moves_counts_as_failure_but_is_charged(settings, ledger):
    client = ScriptedClient(main=Script(san="Qh9", delay_s=0.01), fallback=Script(san="Nf3", delay_s=0.05, tokens=(500, 0, 5)))
    events = await run_think(client, ledger, settings)
    move = final(events)
    assert (move.san, move.source) == ("Nf3", "fallback")
    assert move.usd == usd((1000, 2000, 10), (500, 0, 5))


async def test_panic_under_ten_seconds_runs_fallback_only(settings, ledger):
    client = ScriptedClient(fallback=Script(san="Nf3", delay_s=0.05, tokens=(500, 0, 5)))
    events = await run_think(client, ledger, settings, clock=Clock(ai_ms=8_000, human_ms=8_000, increment_ms=5_000))
    move = final(events)
    assert events[0].level == "none" and events[0].deadline_ms == 2_400
    assert (move.san, move.source) == ("Nf3", "fallback")
    assert client.levels_called == ["minimal"]


async def test_panic_fallback_missing_the_deadline_plays_emergency(settings, ledger):
    client = ScriptedClient(fallback=Script(san="Nf3", delay_s=5))
    events = await run_think(client, ledger, settings, clock=Clock(ai_ms=8_000, human_ms=8_000, increment_ms=5_000))
    move = final(events)
    assert move.source == "emergency"
    assert move.uci == emergency_move(chess.Board()).uci()
    assert "deadline reached" in notes(events)


async def test_emergency_under_two_seconds_makes_no_api_call(settings, ledger):
    client = ScriptedClient()
    events = await run_think(client, ledger, settings, moves=("e2e4",), clock=Clock(1_500, 60_000, 0))
    move = final(events)
    board = board_from_moves(["e2e4"])
    assert move.source == "emergency" and move.uci == emergency_move(board).uci()
    assert client.log == [] and move.usd == 0.0 and ledger.spent_usd == 0.0


async def test_both_calls_failing_plays_emergency_immediately(settings, ledger):
    client = ScriptedClient(
        main=Script(delay_s=0.01, error=RuntimeError("main down")),
        fallback=Script(delay_s=0.01, error=RuntimeError("fallback down")),
    )
    events = await run_think(client, ledger, settings)
    move = final(events)
    assert move.source == "emergency" and move.elapsed_ms < 600
    assert len([n for n in notes(events) if "call failed" in n]) == 2


async def test_spend_cap_reached_yields_only_an_error(settings, tmp_path):
    ledger = SpendLedger(tmp_path / "capped.json")
    ledger.add(settings.cap_usd)
    client = ScriptedClient()
    events = await run_think(client, ledger, settings)
    assert len(events) == 1 and isinstance(events[0], ErrorEvent) and "cap" in events[0].message
    assert client.log == []


async def test_game_over_position_yields_error(settings, ledger):
    mate = ("f2f3", "e7e5", "g2g4", "d8h4")
    events = await run_think(ScriptedClient(), ledger, settings, moves=mate)
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)


async def test_move_is_appended_to_the_moves_log(settings, ledger):
    client = ScriptedClient(main=Script(delay_s=0.01), fallback=Script(san="Nf3", delay_s=0.01, tokens=(500, 0, 5)))
    move = final(await run_think(client, ledger, settings))
    record = json.loads(settings.log_path.read_text().splitlines()[-1])
    assert record["uci"] == move.uci and record["source"] == "model" and record["usd"] == move.usd
    assert record["plan"]["level"] == "medium" and record["fallback_san"] == "Nf3"


async def test_closing_the_stream_cancels_both_tasks_and_charges_finished_calls(settings, ledger):
    client = ScriptedClient(
        main=Script(delay_s=5, thoughts=("hmm",)), fallback=Script(san="Nf3", delay_s=0.01, tokens=(500, 0, 5))
    )
    req = ThinkRequest((), SHORT_DEADLINE_CLOCK, "gemini-3.6-flash")
    events = think(req, client, ledger, settings)
    async for event in events:
        if isinstance(event, ThoughtEvent):
            break
    await events.aclose()
    assert "cancelled:medium" in client.log
    assert ledger.spent_usd == usd((500, 0, 5))
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def test_repeated_cancellation_still_charges_finished_calls(settings, ledger):
    """The web server re-delivers cancellation at every await after a client disconnect."""
    client = ScriptedClient(
        main=Script(delay_s=5, thoughts=("hmm",)), fallback=Script(san="Nf3", delay_s=0.01, tokens=(500, 0, 5))
    )

    async def consume():
        async for _ in think(ThinkRequest((), SHORT_DEADLINE_CLOCK, "gemini-3.6-flash"), client, ledger, settings):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    while not task.done():
        task.cancel()
        await asyncio.sleep(0)
    assert task.cancelled()
    assert ledger.spent_usd == usd((500, 0, 5))
