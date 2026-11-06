import asyncio
import json

import chess
import pytest

from zeitnot.calls import PulseGate
from zeitnot.engines import Answer, Pulse, Rationale
from zeitnot.events import CallEvent, ErrorEvent, MoveEvent, NoteEvent, PlanEvent, PulseEvent, ThoughtEvent
from zeitnot.facts import MISS_LABEL
from zeitnot.player import ThinkRequest, think
from zeitnot.timeman import Clock

from conftest import CLOCK, CRITICAL, FORCED_HISTORY, PRICE_PER_CALL, Script, ScriptedEngine, harness, run_think


def notes(events):
    return [e.text for e in events if isinstance(e, NoteEvent)]


def final(events):
    assert isinstance(events[-1], MoveEvent), events[-1]
    assert sum(isinstance(e, MoveEvent) for e in events) == 1
    return events[-1]


def plan_of(events):
    return next(e for e in events if isinstance(e, PlanEvent))


async def test_forced_move_plays_at_once_without_a_call(settings, ledger):
    engine = ScriptedEngine()
    events = await run_think(engine, ledger, settings, moves=FORCED_HISTORY)
    assert [type(e) for e in events] == [MoveEvent]
    assert (events[0].san, events[0].source, events[0].usd) == ("exd6", "forced", 0.0)
    assert engine.log == []


async def test_easy_position_thinks_at_low_with_no_backup(settings, ledger):
    engine = ScriptedEngine(main=Script(san="e4", thoughts=("**Plan**", " go"), note="Open the centre. Expect ...e5"))
    events = await run_think(engine, ledger, settings)
    plan = plan_of(events)
    assert (plan.level, plan.difficulty, plan.difficulty_text, plan.engine) == ("low", "easy", "scripted easy", "scripted")
    assert [e.text for e in events if isinstance(e, ThoughtEvent)] == ["**Plan**", " go"]
    move = final(events)
    assert (move.san, move.source, move.note, move.ponder_hit) == ("e4", "model", "Open the centre. Expect ...e5", False)
    assert engine.log == ["start:main", "done:main"]
    assert move.usd == PRICE_PER_CALL and ledger.spent_usd == move.usd


async def test_hard_position_thinks_deeper_with_a_backup_in_parallel(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nc3", delay_s=0.1), backup=Script(san="exd5", tokens=(500, 0, 5)))
    events = await run_think(engine, ledger, settings, moves=CRITICAL)
    plan = plan_of(events)
    assert (plan.level, plan.difficulty, plan.blunder_chance) == ("medium", "hard", 0.3)
    assert plan.deadline_ms == 410
    assert "fallback ready: exd5" in notes(events)
    move = final(events)
    assert (move.san, move.source) == ("Nc3", "model")
    assert move.usd == 2 * PRICE_PER_CALL and move.tokens.input == 1500
    assert [c.level for _, c in engine.calls] == ["medium", "low"]


async def test_backup_offers_only_safe_moves_and_main_offers_every_move_untagged(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nc3"), backup=Script(san="exd5"))
    await run_think(engine, ledger, settings, moves=CRITICAL)
    main = next(c for r, c in engine.calls if r == "main")
    backup = next(c for r, c in engine.calls if r == "backup")
    assert "Qg4" in main.choices and "Qg4" not in backup.choices
    assert "Qg4" in main.prompt and "hangs" not in main.prompt


async def test_deadline_plays_a_landed_backup_and_interrupts_main(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nc3", delay_s=5, thoughts=("hmm",)), backup=Script(san="exd5", delay_s=0.01))
    events = await run_think(engine, ledger, settings, moves=CRITICAL)
    move = final(events)
    assert (move.san, move.source) == ("exd5", "fallback")
    assert "deadline reached" in notes(events)
    assert 400 <= move.elapsed_ms < 1_000
    assert "cancelled:main" in engine.log
    assert move.usd == PRICE_PER_CALL


async def test_deadline_without_a_backup_keeps_waiting_for_the_main_answer(settings, ledger):
    engine = ScriptedEngine(main=Script(san="d4", delay_s=0.6))
    events = await run_think(engine, ledger, settings)
    move = final(events)
    assert plan_of(events).deadline_ms == 120
    assert "deadline reached" in notes(events)
    assert (move.san, move.source) == ("d4", "model")
    assert move.elapsed_ms >= 590
    assert "cancelled:main" not in engine.log


async def test_after_the_deadline_the_first_answer_to_land_is_played(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nc3", delay_s=3), backup=Script(san="exd5", delay_s=0.7))
    events = await run_think(engine, ledger, settings, moves=CRITICAL)
    move = final(events)
    assert (move.san, move.source) == ("exd5", "fallback")
    assert 690 <= move.elapsed_ms < 1_500
    assert "cancelled:main" in engine.log


async def test_main_error_plays_the_backup_without_waiting_for_the_deadline(settings, ledger):
    engine = ScriptedEngine(main=Script(delay_s=0.01, error=RuntimeError("503 high demand")), backup=Script(san="exd5", delay_s=0.05))
    events = await run_think(engine, ledger, settings, moves=CRITICAL)
    move = final(events)
    assert (move.san, move.source) == ("exd5", "fallback")
    assert any("main call failed" in n and "503 high demand" in n for n in notes(events))
    assert move.elapsed_ms < 300


async def test_main_error_without_a_backup_asks_the_model_again(settings, ledger):
    engine = ScriptedEngine(main=Script(san="e4", fail_times=1))
    events = await run_think(engine, ledger, settings)
    move = final(events)
    assert (move.san, move.source) == ("e4", "model")
    assert "asking the model again (attempt 2 of 3)" in notes(events)
    assert engine.attempts["main"] == 2


async def test_a_main_call_that_keeps_failing_ends_in_an_error_not_a_move(settings, ledger):
    engine = ScriptedEngine(main=Script(error=RuntimeError("logged out")))
    events = await run_think(engine, ledger, settings)
    assert isinstance(events[-1], ErrorEvent) and "3 times" in events[-1].message
    assert not any(isinstance(e, MoveEvent) for e in events)


async def test_no_rule_overrides_the_models_move(settings, ledger):
    """Qg4 leaves the queen to the c8 bishop by the rules check, but it is the model's answer, so it plays. The rule
    marked the sacrifices Opus missed (Nxe6+) the same way; deciding is the model's job."""
    engine = ScriptedEngine(main=Script(san="Qg4"), backup=Script(san="Nc3"))
    events = await run_think(engine, ledger, settings, moves=CRITICAL)
    move = final(events)
    assert (move.san, move.source) == ("Qg4", "model")
    assert sorted(engine.started_roles) == ["backup", "main"]


async def test_note_hit_and_miss_reach_the_next_prompt(settings, ledger):
    engine = ScriptedEngine(main=Script(san="e4", note="Take the centre.", expected="...e5"))
    h = harness(engine, ledger, settings)
    first = final(await run_think(engine, ledger, settings, h=h))
    assert (first.expected_reply, first.ponder_replies) == ("e5", ("e7e5",))

    events = await run_think(engine, ledger, settings, moves=("e2e4", "e7e5"), h=h)
    hit = engine.prompts("main")[-1]
    assert "<prior_note>\nTake the centre.\nThe opponent played ...e5, as you expected.\n</prior_note>" in hit
    assert "as expected: ...e5" in notes(events)

    events = await run_think(engine, ledger, settings, moves=("e2e4", "d7d5"), h=h)
    miss = engine.prompts("main")[-1]
    assert f"<prior_note>\n{MISS_LABEL} Expected ...e5, opponent played ...d5.\n</prior_note>" in miss
    assert "Take the centre" not in miss
    assert "expected ...e5, got ...d5" in notes(events)


async def test_an_illegal_expected_reply_is_dropped_not_guessed(settings, ledger):
    engine = ScriptedEngine(main=Script(san="e4", note="Centre.", expected="Qxh7#"))
    move = final(await run_think(engine, ledger, settings))
    assert (move.expected_reply, move.ponder_replies) == (None, ())
    answered = next(e for e in await run_think(engine, ledger, settings) if isinstance(e, CallEvent) and e.status == "answered")
    assert answered.expected_reply is None


async def test_note_is_truncated_to_forty_words(settings, ledger):
    from zeitnot.engines import truncate_note

    assert len(truncate_note(" ".join(["word"] * 60)).split()) == 40


async def test_planning_reads_the_live_latency_table(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nc3", delay_s=0.25), backup=Script(san="exd5"))
    h = harness(engine, ledger, settings)
    first = plan_of(await run_think(engine, ledger, settings, moves=CRITICAL, h=h))
    assert first.level == "medium"
    for _ in range(3):
        engine.latency.record("medium", 900)
    later = plan_of(await run_think(engine, ledger, settings, moves=CRITICAL, h=h))
    assert later.level == "low" and engine.latency.p50("medium") == 900


async def test_a_landed_call_updates_the_latency_table(settings, ledger):
    engine = ScriptedEngine(main=Script(san="e4", delay_s=0.12))
    for _ in range(3):
        await run_think(engine, ledger, settings)
    assert 110 <= engine.latency.p50("low") < 400 and engine.latency.snapshot()["low"]["n"] == 3


async def test_spend_cap_stops_a_paid_engine(settings, tmp_path):
    from zeitnot.ledger import SpendLedger

    capped = SpendLedger(tmp_path / "capped.json")
    capped.add(settings.cap_usd)
    engine = ScriptedEngine()
    events = await run_think(engine, capped, settings)
    assert len(events) == 1 and isinstance(events[0], ErrorEvent) and "cap" in events[0].message


async def test_game_over_position_yields_error(settings, ledger):
    events = await run_think(ScriptedEngine(), ledger, settings, moves=("f2f3", "e7e5", "g2g4", "d8h4"))
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)


async def test_move_is_appended_to_the_moves_log(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nc3", note="Defend.", expected="dxe4"), backup=Script(san="exd5"))
    move = final(await run_think(engine, ledger, settings, moves=CRITICAL))
    record = json.loads(settings.log_path.read_text().splitlines()[-1])
    assert record["uci"] == move.uci and record["source"] == "model" and record["note"] == "Defend."
    assert record["plan"]["level"] == "medium" and record["plan"]["difficulty"] == "hard"
    assert record["decision"] == "The model chose Nc3." and record["expected_reply"] == "dxe4"
    assert [c["role"] for c in record["calls"]] == ["main", "backup"] and record["calls"][0]["status"] == "chosen"


async def test_closing_the_stream_cancels_calls_and_charges_finished_ones(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nc3", delay_s=5, thoughts=("hmm",)), backup=Script(san="exd5", delay_s=0.01))
    h = harness(engine, ledger, settings)
    events = think(ThinkRequest(CRITICAL, CLOCK, engine.name), h)
    async for event in events:
        if isinstance(event, ThoughtEvent):
            break
    await events.aclose()
    assert "cancelled:main" in engine.log
    assert ledger.spent_usd == PRICE_PER_CALL
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def test_repeated_cancellation_still_charges_finished_calls(settings, ledger):
    """The web server re-delivers cancellation at every await after a client disconnect."""
    engine = ScriptedEngine(main=Script(san="Nc3", delay_s=5, thoughts=("hmm",)), backup=Script(san="exd5", delay_s=0.01))
    h = harness(engine, ledger, settings)

    async def consume():
        async for _ in think(ThinkRequest(CRITICAL, CLOCK, engine.name), h):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    while not task.done():
        task.cancel()
        await asyncio.sleep(0)
    assert task.cancelled()
    assert ledger.spent_usd == PRICE_PER_CALL


def test_no_move_comes_from_code():
    import zeitnot.events as events
    import zeitnot.rules as rules

    assert not hasattr(rules, "emergency_move") and "emergency" not in events.Source.__args__
    assert set(events.Source.__args__) == {"model", "fallback", "forced"}
    assert chess.Board().legal_moves.count() == 20


class Clockwork:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_pulse_gate_lets_one_pulse_through_per_interval():
    clock = Clockwork()
    gate = PulseGate(0.25, clock)
    seen = []
    for step in range(20):
        clock.now = step * 0.05
        seen.append(gate.ready())
    assert [i * 0.05 for i, ok in enumerate(seen) if ok] == pytest.approx([0.0, 0.25, 0.5, 0.75])


class Chatty(ScriptedEngine):
    """Pulses every 10 ms for 0.6 s, then streams its commentary in three pieces. One level, so no backup call."""

    main_levels = ("low",)

    async def stream(self, call):
        self.calls.append(("main", call))
        for _ in range(60):
            await asyncio.sleep(0.01)
            yield Pulse()
        for piece in ("Nf3 keeps ", "e5 covered", "."):
            yield Rationale(piece)
        yield Answer("Nf3", "Expect ...Nc6", 10, 0, 1, usd=PRICE_PER_CALL)


async def test_pulses_are_rate_limited_and_commentary_streams_with_one_id(settings, ledger):
    events = await run_think(Chatty(), ledger, settings, clock=Clock(60_000, 60_000, 0))
    pulses = [e for e in events if isinstance(e, PulseEvent)]
    assert 2 <= len(pulses) <= 4, "60 pulses in 0.6 s become at most one per 0.25 s"
    pieces = [e for e in events if isinstance(e, ThoughtEvent)]
    assert "".join(e.text for e in pieces) == "Nf3 keeps e5 covered." and len({e.stream for e in pieces}) == 1
    assert pieces[0].stream is not None and final(events).san == "Nf3"


async def test_every_calls_thoughts_and_pulses_carry_its_call_id(settings, ledger):
    engine = ScriptedEngine(main=Script(san="Nc3", delay_s=0.1, thoughts=("main thinking",)),
                            backup=Script(san="exd5", thoughts=("backup thinking",)))
    events = await run_think(engine, ledger, settings, moves=CRITICAL)
    ids = {e.role: e.call for e in events if isinstance(e, CallEvent) and e.status == "started"}
    thoughts = {e.text: e.call for e in events if isinstance(e, ThoughtEvent)}
    assert thoughts == {"main thinking": ids["main"], "backup thinking": ids["backup"]}
    assert {e.call for e in events if isinstance(e, PulseEvent)} <= set(ids.values())
