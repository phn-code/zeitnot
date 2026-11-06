import importlib.util
import json
from pathlib import Path

import pytest

from zeitnot.engines import FakeEngine

RUN = Path(__file__).resolve().parents[1] / "experiments" / "harness_eval" / "run.py"
POSITIONS = [
    {"id": "q1", "fen": "", "history": ["e2e4", "d7d5"], "human": "exd5", "group": "critical"},
    {"id": "q2", "fen": "", "history": ["e2e4", "e7e5"], "human": "Nf3", "group": "quiet"},
]


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("harness_eval_run", RUN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Args:
    max_gemini_usd, codex_workers, gemini_workers, claude_workers, timeout, opinions = 1.0, 2, 2, 2, 30, False


def calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_calls_are_recorded_once_and_never_repeated(run, tmp_path):
    path = tmp_path / "calls.jsonl"
    engines = {"codex": FakeEngine({"low": 0})}
    await run.run_calls(POSITIONS, engines, Args, path)
    first = calls(path)
    assert {(r["pid"], r["kind"]) for r in first if r["kind"] != "verify"} == {(p["id"], k) for p in POSITIONS for k in ("v1", "v2")}
    assert all("error" not in r for r in first)
    await run.run_calls(POSITIONS, engines, Args, path)
    assert calls(path) == first


async def test_the_gemini_cap_is_a_hard_stop(run, tmp_path):
    path = tmp_path / "calls.jsonl"

    class Capped(Args):
        max_gemini_usd, gemini_workers = 0.051, 1

    await run.run_calls(POSITIONS, {"gemini": FakeEngine({"low": 0})}, Capped, path)
    made = [r for r in calls(path) if r["engine"] == "gemini"]
    assert 1 <= len(made) < 4 and sum(r["usd"] for r in made) <= Capped.max_gemini_usd


def test_v1_prompt_is_the_frozen_baseline(run):
    import chess

    text = run.v1_prompt(chess.Board(), ["e4", "d4"])
    assert text.endswith("White to move.\n\nLegal moves: e4, d4") and "a b c d e f g h" in text


async def test_resume_retries_errored_records_and_keeps_good_ones(run, tmp_path):
    path = tmp_path / "calls.jsonl"
    good = {"pid": "q1", "engine": "codex", "kind": "v2", "move": "Nc3", "note": None, "seconds": 9.0, "usd": 0.0, "tokens": None}
    bad = {"pid": "q1", "engine": "codex", "kind": "v1", "error": "ValueError: no legal answer: '{\"move\":\"Qd8\"}'",
           "seconds": 30.0, "usd": 0.0, "tokens": None}
    path.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
    await run.run_calls(POSITIONS, {"codex": FakeEngine({"low": 0})}, Args, path)
    rows = calls(path)
    assert rows[:2] == [good, bad], "earlier records are kept as they were"
    q1 = [(r["kind"], "error" in r) for r in rows if r["pid"] == "q1"]
    assert q1.count(("v2", False)) == 1, "a good record is not called again"
    assert ("v1", False) in q1, "an errored record is called again"


async def test_the_opinions_arm_reuses_v2_adds_a_second_call_escalates_splits_and_resumes(run, tmp_path):
    import chess

    path = tmp_path / "calls.jsonl"
    engines = {"claude": FakeEngine({"low": 0})}
    await run.run_calls(POSITIONS, engines, Args, path)
    before = calls(path)

    class WithOpinions(Args):
        opinions = True

    await run.run_calls(POSITIONS, engines, WithOpinions, path)
    rows = calls(path)
    assert rows[:len(before)] == before, "existing records are untouched"
    new = rows[len(before):]
    seconds = [r for r in new if r["kind"] == "second"]
    assert [r["pid"] for r in seconds] == ["q1"] and seconds[0]["prompt"] == run.PROMPT_C, "critical positions only"
    v2 = next(r for r in before if (r["pid"], r["kind"]) == ("q1", "v2"))
    escalations = [r for r in new if r["kind"] == "escalate"]
    assert len(escalations) == (seconds[0]["move"] != v2["move"])
    if escalations:
        esc = escalations[0]
        assert (esc["main"], esc["second"]) == (v2["move"], seconds[0]["move"]) and esc["calls"] == 3
        assert esc["move"] in (v2["move"], seconds[0]["move"]) and esc["move"] == esc["pick"]
        assert esc["decision"].startswith(f"Opinions split ({v2['move']} vs {seconds[0]['move']})")
    await run.run_calls(POSITIONS, engines, WithOpinions, path)
    assert calls(path) == rows, "a rerun makes no new calls"

    records = {(r["pid"], r["engine"], r["kind"]): r for r in rows}
    board = chess.Board()
    for uci in POSITIONS[0]["history"]:
        board.push_uci(uci)
    grades = {"q1": {"best": 50.0, "win": {board.san(m): 50.0 for m in board.legal_moves}},
              "q2": {"best": 50.0, "win": {}}}
    summary = "\n".join(run.opinions_summary(POSITIONS, records, grades, "claude"))
    assert "Disagreement: " in summary and "Loss against v2, paired" in summary and "+0.0 (+0.0 to +0.0)" in summary
