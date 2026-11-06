import asyncio
import time

import chess

from zeitnot.events import CallEvent, DecisionEvent, MoveEvent, PlanEvent, ThoughtEvent
from zeitnot.player import ThinkRequest, one_move
from zeitnot.rules import board_from_moves

from conftest import CLOCK, CRITICAL, PRICE_PER_CALL, Script, ScriptedEngine, harness, run_think

AFTER_E4_E5 = ("e2e4", "e7e5")


def fen_of(moves):
    return board_from_moves(moves).fen()


def ponder(h, engine, moves):
    """Ponders the position after `moves`, whose last move is the opponent's expected reply."""
    req = ThinkRequest(tuple(moves), CLOCK, engine.name)
    before = board_from_moves(moves[:-1])
    reply = before.san(chess.Move.from_uci(moves[-1]))
    return h.ponder.start(engine.name, fen_of(moves), before.fen(), lambda: one_move(req, h, time.monotonic(), ponder=reply))


async def test_a_finished_ponder_run_is_played_at_once_as_a_ponder_hit(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nf3", thoughts=("**Prepared**",), note="Develop.", expected="...Nc6"))
    h = harness(engine, ledger, settings, ponder=True)
    assert ponder(h, engine, AFTER_E4_E5) == "started"
    await h.ponder.runs[("scripted", fen_of(AFTER_E4_E5))].task
    assert ponder(h, engine, AFTER_E4_E5) == "ready"

    events = await run_think(engine, ledger, settings, moves=AFTER_E4_E5, h=h)
    move = events[-1]
    assert isinstance(move, MoveEvent) and move.ponder_hit and (move.san, move.source) == ("Nf3", "model")
    assert move.elapsed_ms < 50 and move.note == "Develop." and move.expected_reply == "Nc6"
    assert any(isinstance(e, ThoughtEvent) and e.text == "**Prepared**" for e in events) and isinstance(events[0], PlanEvent)
    decision = events[-2]
    assert isinstance(decision, DecisionEvent) and decision.text == "You played e5 as expected, so the prepared Nf3 plays at once."
    root = next(e for e in events if isinstance(e, CallEvent))
    assert (root.role, root.label, root.parent) == ("ponder", "If you play e5", None) and decision.call == root.call
    assert engine.attempts["main"] == 1
    assert ledger.spent_usd == PRICE_PER_CALL
    assert h.notes.get(AFTER_E4_E5 + ("g1f3",)).expected == chess.Move.from_uci("b8c6")


async def test_a_ponder_run_in_progress_is_awaited_not_restarted(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nf3", delay_s=0.3, thoughts=("live",)))
    h = harness(engine, ledger, settings, ponder=True)
    ponder(h, engine, AFTER_E4_E5)
    await asyncio.sleep(0.05)
    assert ponder(h, engine, AFTER_E4_E5) == "running"

    events = await run_think(engine, ledger, settings, moves=AFTER_E4_E5, h=h)
    move = events[-1]
    assert move.ponder_hit and move.san == "Nf3" and 200 <= move.elapsed_ms < 600
    assert "live" in [e.text for e in events if isinstance(e, ThoughtEvent)]
    assert engine.attempts["main"] == 1


async def test_think_for_another_position_cancels_the_stale_ponder_run(settings, ledger):
    engine = ScriptedEngine(main=Script(san="d4", delay_s=2))
    h = harness(engine, ledger, settings, ponder=True)
    ponder(h, engine, AFTER_E4_E5)
    await asyncio.sleep(0.05)
    stale = h.ponder.runs[("scripted", fen_of(AFTER_E4_E5))]
    engine.scripts["main"] = Script(san="Nc3")
    events = await run_think(engine, ledger, settings, moves=CRITICAL, h=h)
    await asyncio.gather(stale.task, return_exceptions=True)
    assert stale.task.cancelled() and ("scripted", fen_of(AFTER_E4_E5)) not in h.ponder.runs
    assert "cancelled:main" in engine.log
    assert not events[-1].ponder_hit


async def test_replies_of_one_turn_ponder_side_by_side_and_another_turn_cancels_them(settings, ledger):
    engine = ScriptedEngine(main=Script(san="d4", delay_s=2))
    h = harness(engine, ledger, settings, ponder=True)
    ponder(h, engine, AFTER_E4_E5)
    ponder(h, engine, ("e2e4", "c7c5"))
    await asyncio.sleep(0.05)
    first, second = (h.ponder.runs[("scripted", fen_of(m))] for m in (AFTER_E4_E5, ("e2e4", "c7c5")))
    assert not first.done and not second.done, "two replies to 1. e4 ponder at once"
    ponder(h, engine, ("d2d4", "d7d5"))
    await asyncio.gather(first.task, second.task, return_exceptions=True)
    assert first.task.cancelled() and second.task.cancelled() and list(h.ponder.runs) == [("scripted", fen_of(("d2d4", "d7d5")))]
    await h.ponder.close()


async def test_a_prepared_move_plays_even_when_a_rule_would_flag_it(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Qg4"), backup=Script(san="exd5"))
    h = harness(engine, ledger, settings, ponder=True)
    ponder(h, engine, CRITICAL)
    await h.ponder.runs[("scripted", fen_of(CRITICAL))].task
    engine.scripts["main"] = Script(san="Nc3")
    move = (await run_think(engine, ledger, settings, moves=CRITICAL, h=h))[-1]
    assert (move.san, move.ponder_hit) == ("Qg4", True)


async def test_pondering_uses_the_note_from_the_move_before(settings, ledger):
    engine = ScriptedEngine(main=Script(san="e4", note="Centre.", expected="e5"))
    h = harness(engine, ledger, settings, ponder=True)
    await run_think(engine, ledger, settings, h=h)
    ponder(h, engine, AFTER_E4_E5)
    await h.ponder.runs[("scripted", fen_of(AFTER_E4_E5))].task
    assert "as you expected" in engine.prompts("main")[-1]
    assert chess.Board(fen_of(AFTER_E4_E5)).turn == chess.WHITE
