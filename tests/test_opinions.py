import asyncio
import json
import time
from dataclasses import replace

import chess
import pytest
from fastapi.testclient import TestClient

from zeitnot.config import Settings
from zeitnot.context import FORCING_TASK, TASK
from zeitnot.engines import Answer, Rationale
from zeitnot.events import CallEvent, DecisionEvent, MoveEvent, NoteEvent, PlanEvent, PulseEvent, ThoughtEvent
from zeitnot.player import ThinkRequest, one_move
from zeitnot.rules import board_from_moves
from zeitnot.server import create_app

from zeitnot import compare
from conftest import CLOCK, CRITICAL, FAST_LATENCY, PRICE_PER_CALL, ScriptedEngine, harness, run_think

FEN = board_from_moves(CRITICAL).fen()


def after(san):
    board = board_from_moves(CRITICAL)
    board.push_san(san)
    return board.fen()


class OpinionEngine(ScriptedEngine):
    """Answers by the kind of call: main, second opinion, backup, a reply after a candidate, or the pick."""

    def __init__(self, main="Nc3", second="exd5", backup="Nc3", replies=None, pick="exd5", delays=None, latency=None):
        super().__init__(latency=latency or FAST_LATENCY)
        self.moves = {"main": main, "second": second, "backup": backup, "pick": pick}
        self.replies = {after(san): reply for san, reply in (replies or {"Nc3": "dxe4", "exd5": "Qxd5"}).items()}
        self.delays = {"main": 0.02, "second": 0.04, "backup": 0.01, "reply": 0.02, "pick": 0.02, **(delays or {})}
        self.answered: list[tuple[str, str]] = []

    def kind(self, call):
        if "<lines>" in call.prompt:
            return "pick", self.moves["pick"]
        if call.fen in self.replies:
            return "reply", self.replies[call.fen]
        if call.prompt.endswith(FORCING_TASK):
            return "second", self.moves["second"]
        assert call.prompt.endswith(TASK)
        return ("main", self.moves["main"]) if call.level == "medium" else ("backup", self.moves["backup"])

    async def stream(self, call):
        kind, san = self.kind(call)
        self.calls.append((kind, call))
        self.log.append(f"start:{kind}")
        try:
            await asyncio.sleep(self.delays[kind])
            yield Rationale(f"{kind} likes {san}. ")
            self.answered.append((kind, san))
            yield Answer(san, f"{kind} plan.", 10, 0, 1, usd=PRICE_PER_CALL, expected_reply="Bf5" if kind == "pick" else None)
        except asyncio.CancelledError:
            self.log.append(f"cancelled:{kind}")
            raise


def calls_of(events):
    """Call id -> its lifecycle as [(status, move)], and id -> its started event."""
    life, started = {}, {}
    for e in events:
        if isinstance(e, CallEvent):
            life.setdefault(e.call, []).append((e.status, e.move))
            if e.status == "started":
                started[e.call] = e
    return life, started


def by_label(events):
    _, started = calls_of(events)
    return {e.label: e for e in started.values()}


def decision_of(events):
    assert isinstance(events[-1], MoveEvent) and isinstance(events[-2], DecisionEvent)
    return events[-2]


def kinds(engine):
    return [k for k, _ in engine.calls]


async def test_agreeing_opinions_play_the_move_and_both_are_chosen(settings, ledger):
    engine = OpinionEngine(second="Nc3")
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    plan = next(e for e in events if isinstance(e, PlanEvent))
    assert plan.opinions and plan.difficulty == "hard"
    assert "reply" not in kinds(engine) and "pick" not in kinds(engine)
    decision = decision_of(events)
    assert (decision.text, decision.move, events[-1].san) == ("Both opinions chose Nc3.", "Nc3", "Nc3")
    life, started = calls_of(events)
    labels = by_label(events)
    main, second = labels["Main"], labels["Second opinion · most forcing"]
    assert (main.role, main.parent, main.framing, main.level, main.engine) == ("main", None, "plain", "medium", "scripted")
    assert (second.role, second.parent, second.framing, second.level) == ("second_opinion", None, "most forcing", "medium")
    assert life[main.call] == [("started", None), ("answered", "Nc3"), ("chosen", "Nc3")]
    assert life[second.call] == [("started", None), ("answered", "Nc3"), ("chosen", "Nc3")]
    assert decision.call == main.call


async def test_a_split_with_time_left_plays_both_replies_then_the_pick(settings, ledger):
    engine = OpinionEngine()
    events = await run_think(engine, ledger, replace(settings, ponder_replies=2), moves=CRITICAL, opinions=True)
    labels = by_label(events)
    main, second = labels["Main"], labels["Second opinion · most forcing"]
    to_main, to_second = labels["Your best reply to Nc3"], labels["Your best reply to exd5"]
    pick = labels["Pick · Nc3 or exd5"]
    assert (to_main.role, to_main.parent, to_main.fen) == ("reply", main.call, after("Nc3"))
    assert (to_second.role, to_second.parent, to_second.fen) == ("reply", second.call, after("exd5"))
    assert (pick.role, pick.parent, pick.fen) == ("pick", None, FEN)
    answered = {e.call: e for e in events if isinstance(e, CallEvent) and e.status == "answered"}
    assert answered[to_main.call].move == "dxe4" and answered[to_second.call].move == "Qxd5"
    assert any(f.startswith("material: you") for f in answered[to_main.call].facts), "reply cards carry the facts after the reply"
    pick_prompt = next(c.prompt for k, c in engine.calls if k == "pick")
    assert "Other players suggest Nc3 and exd5" in pick_prompt and "Line 1: Nc3, the opponent's strongest reply found: ...dxe4" in pick_prompt
    assert next(c for k, c in engine.calls if k == "pick").choices == ("Nc3", "exd5")
    decision = decision_of(events)
    assert decision.text == "Opinions split (Nc3 vs exd5). After Nc3 you would play dxe4, so exd5."
    assert decision.call == pick.call
    life, _ = calls_of(events)
    assert life[pick.call][-1] == ("chosen", "exd5") and life[second.call][-1] == ("chosen", "exd5")
    assert life[main.call][-1] == ("answered", "Nc3")
    move = events[-1]
    assert (move.san, move.source, move.note, move.expected_reply) == ("exd5", "model", "pick plan.", "Bf5")
    assert move.ponder_replies == ("c8f5", "d8d5"), "the pick's expected reply, then the reply the analysis found"
    assert move.usd == 6 * PRICE_PER_CALL


async def test_only_the_expected_reply_is_pondered_by_default(settings, ledger):
    events = await run_think(OpinionEngine(), ledger, settings, moves=CRITICAL, opinions=True)
    assert events[-1].ponder_replies == ("c8f5",)


def test_ponder_replies_and_ponder_opinions_are_read_from_the_environment(monkeypatch):
    for var in ("ZEITNOT_PONDER_REPLIES", "ZEITNOT_PONDER_OPINIONS"):
        monkeypatch.delenv(var, raising=False)
    assert (Settings.from_env().ponder_replies, Settings.from_env().ponder_opinions) == (1, True)
    monkeypatch.setenv("ZEITNOT_PONDER_REPLIES", "2")
    monkeypatch.setenv("ZEITNOT_PONDER_OPINIONS", "0")
    assert (Settings.from_env().ponder_replies, Settings.from_env().ponder_opinions) == (2, False)


async def test_a_split_without_time_for_another_round_plays_the_main_answer(settings, ledger):
    slow_low = {"low": (5_000, 9_000), "medium": (100, 300)}
    engine = OpinionEngine(latency=slow_low, delays={"second": 0.005})
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    assert "reply" not in kinds(engine) and "pick" not in kinds(engine)
    assert decision_of(events).text == "Opinions split (Nc3 vs exd5) with no time to compare, so the main answer Nc3."
    assert events[-1].san == "Nc3"
    assert "opinions split (Nc3 vs exd5) with no time to compare them" in [e.text for e in events if isinstance(e, NoteEvent)]


async def test_a_split_with_time_for_one_round_runs_the_quick_pick(settings, ledger):
    one_round = {"low": (250, 300), "medium": (100, 300)}
    engine = OpinionEngine(latency=one_round)
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    assert "reply" not in kinds(engine) and kinds(engine).count("pick") == 1
    assert "opinions split (Nc3 vs exd5); time for a quick comparison" in [e.text for e in events if isinstance(e, NoteEvent)]
    pick_prompt = next(c.prompt for k, c in engine.calls if k == "pick")
    assert "Line 1: Nc3. After it the opponent can play: " in pick_prompt and "dxe4" in pick_prompt
    assert pick_prompt.endswith(compare.QUICK_PICK_TASK)
    assert decision_of(events).text == "Opinions split (Nc3 vs exd5); the pick chose exd5."
    assert events[-1].san == "exd5"


async def test_the_second_opinion_thinks_one_level_deeper_and_the_main_answer_covers_it(settings, ledger):
    deeper = {"low": (50, 100), "medium": (100, 300), "high": (150, 900)}
    engine = OpinionEngine(second="Nc3", latency=deeper)
    engine.main_levels = ("low", "medium", "high")
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    plan = next(e for e in events if isinstance(e, PlanEvent))
    assert (plan.level, plan.second_level) == ("medium", "high"), "high's 900 ms slow case misses the 410 ms deadline"
    assert {k: c.level for k, c in engine.calls if k in ("main", "second")} == {"main": "medium", "second": "high"}

    slow = OpinionEngine(latency=deeper, delays={"second": 2.0})
    slow.main_levels = ("low", "medium", "high")
    events = await run_think(slow, ledger, settings, moves=CRITICAL, opinions=True)
    assert events[-1].san == "Nc3" and events[-1].elapsed_ms < 400, "waits for the deeper opinion only until the budget"
    assert decision_of(events).text == "The second opinion did not answer in time, so the main answer Nc3."


async def test_no_wait_for_a_second_opinion_that_could_not_change_the_move(settings, ledger):
    slow_low = {"low": (5_000, 9_000), "medium": (100, 300)}
    engine = OpinionEngine(latency=slow_low, delays={"second": 0.3})
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    assert decision_of(events).text == "The second opinion did not answer in time, so the main answer Nc3."
    assert events[-1].elapsed_ms < 150, "not even a quick comparison fits, so a later answer could not change the move"
    assert "deadline reached" not in [e.text for e in events if isinstance(e, NoteEvent)]


async def test_replies_that_miss_the_deadline_fall_back_to_the_main_answer(settings, ledger):
    engine = OpinionEngine(delays={"reply": 2.0})
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    assert events[-1].san == "Nc3" and events[-1].elapsed_ms < 1_000
    assert decision_of(events).text == "Opinions split (Nc3 vs exd5); no time was left to compare the lines, so the main answer Nc3."
    life, started = calls_of(events)
    replies = [cid for cid, e in started.items() if e.role == "reply"]
    assert len(replies) == 2 and all(life[cid][-1] == ("cancelled", None) for cid in replies)
    assert "pick" not in kinds(engine)


async def test_a_pick_that_misses_the_deadline_falls_back_to_the_main_answer(settings, ledger):
    engine = OpinionEngine(delays={"pick": 2.0})
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    assert events[-1].san == "Nc3" and events[-1].elapsed_ms < 1_000
    assert decision_of(events).text == "Opinions split (Nc3 vs exd5); the pick did not finish, so the main answer Nc3."
    pick = by_label(events)["Pick · Nc3 or exd5"]
    assert calls_of(events)[0][pick.call][-1] == ("cancelled", None)


async def test_a_second_opinion_that_misses_the_deadline_is_cancelled(settings, ledger):
    engine = OpinionEngine(delays={"second": 2.0})
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    assert decision_of(events).text == "The second opinion did not answer in time, so the main answer Nc3."
    second = by_label(events)["Second opinion · most forcing"]
    assert calls_of(events)[0][second.call][-1] == ("cancelled", None) and "cancelled:second" in engine.log


SCENARIOS = {
    "agree": dict(second="Nc3"),
    "split and pick": dict(),
    "split, pick keeps main": dict(pick="Nc3"),
    "split, no time": dict(latency={"low": (5_000, 9_000), "medium": (100, 300)}),
    "slow replies": dict(delays={"reply": 2.0}),
    "slow pick": dict(delays={"pick": 2.0}),
    "slow second": dict(delays={"second": 2.0}),
    "main past the deadline": dict(delays={"main": 2.0, "second": 2.0}),
}


@pytest.mark.parametrize("scenario", SCENARIOS.values(), ids=SCENARIOS.keys())
async def test_no_path_plays_a_move_that_no_model_call_answered(settings, ledger, scenario):
    engine = OpinionEngine(**scenario)
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    move, decision = events[-1], decision_of(events)
    assert move.san in {san for kind, san in engine.answered if kind != "reply"}
    answered = {e.call: e.move for e in events if isinstance(e, CallEvent) and e.status == "answered" and e.fen == FEN}
    assert answered[decision.call] == move.san == decision.move
    chosen = [e for e in events if isinstance(e, CallEvent) and e.status == "chosen"]
    assert chosen and all(e.move == move.san for e in chosen)



async def test_quiet_positions_and_the_off_switch_ask_for_no_second_opinion(settings, ledger):
    engine = OpinionEngine(backup="e4")  # a quiet move thinks at low, which this engine reads as the backup level
    quiet = await run_think(engine, ledger, settings, opinions=True)
    assert not next(e for e in quiet if isinstance(e, PlanEvent)).opinions and len(engine.calls) == 1
    assert decision_of(quiet).text == "The model chose e4."
    engine = OpinionEngine()
    off = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=False)
    assert "second" not in kinds(engine) and decision_of(off).text == "The model chose Nc3."


def test_opinions_are_on_by_default_and_zeitnot_opinions_0_turns_them_off(monkeypatch):
    monkeypatch.delenv("ZEITNOT_OPINIONS", raising=False)
    assert Settings.from_env().opinions is True
    monkeypatch.setenv("ZEITNOT_OPINIONS", "0")
    assert Settings.from_env().opinions is False


async def test_thoughts_and_pulses_carry_the_call_id_of_the_call_that_made_them(settings, ledger):
    engine = OpinionEngine()
    events = await run_think(engine, ledger, settings, moves=CRITICAL, opinions=True)
    _, started = calls_of(events)
    for e in events:
        if isinstance(e, ThoughtEvent):
            assert e.call in started and e.stream == e.call
            kind = e.text.split()[0]
            assert {"main": "main", "second": "second_opinion", "backup": "backup", "reply": "reply", "pick": "pick"}[kind] == started[e.call].role
    assert all(e.call in started for e in events if isinstance(e, PulseEvent))
    order = [e for e in events if isinstance(e, CallEvent)]
    assert all(order.index(next(x for x in order if x.call == e.call)) <= order.index(e) for e in order), "started comes first"


async def test_a_ponder_run_is_one_tree_under_its_ponder_call(settings, ledger):
    engine = OpinionEngine()
    h = harness(engine, ledger, replace(settings, ponder_opinions=True), ponder=True)
    req = ThinkRequest(CRITICAL, CLOCK, engine.name, opinions=True)
    events = [e async for e in one_move(req, h, time.monotonic(), ponder="d5")]
    _, started = calls_of(events)
    roots = [e for e in started.values() if e.parent is None]
    assert [(e.role, e.label) for e in roots] == [("ponder", "If you play d5")]
    root = roots[0].call
    tops = {e.label: e.parent for e in started.values() if e.role in ("second_opinion", "backup", "pick")}
    assert tops == {"Second opinion · most forcing": root, "Backup · safe moves only": root, "Pick · Nc3 or exd5": root}
    assert by_label(events)["Your best reply to Nc3"].parent == root, "the reply hangs under the call that proposed Nc3"


async def test_a_hard_ponder_run_asks_the_second_opinion_on_the_opponents_time(settings, ledger):
    engine = OpinionEngine(second="Nc3")
    h = harness(engine, ledger, settings, ponder=True)
    req = ThinkRequest(CRITICAL, CLOCK, engine.name, opinions=True)
    events = [e async for e in one_move(req, h, time.monotonic(), ponder="d5")]
    assert next(e for e in events if isinstance(e, PlanEvent)).opinions
    assert sorted(kinds(engine)) == ["backup", "main", "second"]


async def test_ponder_opinions_can_be_turned_off(settings, ledger):
    engine = OpinionEngine()
    h = harness(engine, ledger, replace(settings, ponder_opinions=False), ponder=True)
    req = ThinkRequest(CRITICAL, CLOCK, engine.name, opinions=True)
    events = [e async for e in one_move(req, h, time.monotonic(), ponder="d5")]
    assert not next(e for e in events if isinstance(e, PlanEvent)).opinions
    assert sorted(kinds(engine)) == ["backup", "main"]


def test_the_ponder_stream_shows_the_run_live_and_never_sends_its_move(tmp_path):
    settings = Settings(fake=True, fake_delay_s={"minimal": 0.01, "low": 0.04, "medium": 0.15, "high": 0.15},
                        ledger_path=tmp_path / "spend.json")
    clock = {"ai_ms": 600_000, "human_ms": 598_000, "increment_ms": 5_000}
    with TestClient(create_app(settings)) as c:
        r = c.post("/api/ponder", json={"moves": ["e2e4"], "clock": clock, "expected_reply": "d7d5"})
        fen = r.json()["fen"]
        with c.stream("GET", "/api/ponder/stream", params={"fen": fen, "engine": "fake"}) as s:
            assert s.status_code == 200
            events = [json.loads(line) for line in s.iter_lines() if line]
        assert c.get("/api/ponder/stream", params={"fen": chess.Board().fen(), "engine": "fake"}).status_code == 404
        assert c.get("/api/ponder/stream", params={"fen": fen, "engine": "claude"}).status_code == 404, "runs belong to one engine"
    types = [e["type"] for e in events]
    assert types[0] == "plan" and "move" not in types and types[-1] == "decision"
    root = next(e for e in events if e["type"] == "call")
    assert (root["role"], root["label"], root["status"]) == ("ponder", "If you play d5", "started")
    assert events[0]["opinions"] is False, "a ponder run asks no second opinion unless ZEITNOT_PONDER_OPINIONS=1"
    assert not any(e["type"] == "call" and e["role"] == "second_opinion" for e in events)
